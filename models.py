import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from torchtext import data
from torchtext import datasets

from sklearn.model_selection import train_test_split
from sklearn.metrics import f1_score

import spacy
import numpy as np
import pandas as pd

import matplotlib.pyplot as plt
import seaborn as sns

import time
import math
import random
from itertools import chain
from tqdm import tqdm


class Encoder(nn.Module):
    def __init__(self, input_dim, emb_dim, hid_dim, n_layers, dropout, bidirectional=True):
        super().__init__()

        self.hid_dim = hid_dim
        self.n_layers = n_layers
        self.bidirectional = bidirectional

        self.embedding = nn.Embedding(input_dim, emb_dim)

        # self.rnn = nn.LSTM(emb_dim, hid_dim, n_layers, bidirectional=bidirectional, dropout=dropout)

        # Switching to a GRU, mainly because it is what the example in the text book uses
        # and the outputs for LSTMs and GRUs are different
        self.rnn = nn.GRU(emb_dim, hid_dim, n_layers, bidirectional=bidirectional, dropout=dropout)

        self.fc = nn.Linear(hid_dim * 2, hid_dim)

        self.dropout = nn.Dropout(dropout)

    def forward(self, src):
        # src = [src len, batch size]

        embedded = self.dropout(self.embedding(src))
        # embedded = [src len, batch size, emb dim]

        outputs, hidden = self.rnn(embedded)
        # outputs = [src len, batch size, hid dim * n directions (2)]
        # hidden = [n layers * n directions, batch size, hid dim]
        # cell = [n layers * n directions, batch size, hid dim]

        hidden = torch.tanh(self.fc(torch.cat(
            (hidden[-2, :, :], hidden[-1, :, :]), dim=1)))

        return outputs, hidden

class Attention(nn.Module):
    def __init__(self, hid_dim):
        super().__init__()

        self.attn = nn.Linear(hid_dim * 3, hid_dim)
        self.v = nn.Linear(hid_dim, 1, bias=False)

    def forward(self, hidden, enc_outputs):
        # hidden = [batch size, hid dim]
        # enc_outputs = [src len, batch size, hid dim * 2]

        src_len = enc_outputs.shape[0]

        hidden = hidden.unsqueeze(1).repeat(1, src_len, 1)
        # hidden = [batch size, src len, hid dim]

        enc_outputs = enc_outputs.permute(1, 0, 2)
        # enc_outputs = [batch size, src len, hid dim * 2]

        energy = torch.tanh(self.attn(torch.cat((hidden, enc_outputs), dim=2)))
        # energy = [batch size, src len, hid dim]

        attention = self.v(energy).squeeze(2)
        # attention = [batch size, src len]

        return F.softmax(attention, dim=1)


class Decoder(nn.Module):
    def __init__(self, output_dim, emb_dim, hid_dim, n_layers, dropout, attention, bidirectional=True):
        super().__init__()

        self.output_dim = output_dim
        self.hid_dim = hid_dim
        self.n_layers = n_layers
        self.bidirectional = bidirectional

        self.attention = attention

        self.embedding = nn.Embedding(output_dim, emb_dim)

        # self.rnn = nn.LSTM((hid_dim * 2) + emb_dim, hid_dim, num_layers=n_layers, bidirectional=bidirectional, dropout=dropout)

        self.rnn = nn.GRU((hid_dim * 2) + emb_dim, hid_dim)

        self.fc_out = nn.Linear((hid_dim * 3) + emb_dim, output_dim)

        self.dropout = nn.Dropout(dropout)

    def forward(self, input, outputs, hidden):
        # input = [batch size]
        # outputs = [src len, batch size, emb dim]
        # hidden = [batch size, emb dim]
        # cell = [n layers * n directions, batch size, hid dim]

        input = input.unsqueeze(0)
        # input = [1, batch size]

        embedded = self.dropout(self.embedding(input))
        # embedded = [1, batch size, emb dim]

        a = self.attention(hidden, outputs)
        # attention = [batch size, src len]

        a = a.unsqueeze(1)
        # attention = [batch size, 1, src len]

        enc_outputs = outputs.permute(1, 0, 2)
        # enc_outputs = [batch size, src len, hid dim * 2]

        weighted = torch.bmm(a, enc_outputs)
        # weighted = [batch size, 1, hid dim * 2]

        weighted = weighted.permute(1, 0, 2)
        # weighted = [1, batch size, hid dim * 2]

        rnn_input = torch.cat((embedded, weighted), dim=2)
        # rnn_input = [1, batch size, hid dim * 3]

        output, hidden = self.rnn(rnn_input, hidden.unsqueeze(0))
        # output = [1, batch size, emb dim]
        # hidden = [1, batch size, emb dim]

        embedded = embedded.squeeze(0)
        # embedded = [batch size, emb dim]
        output = output.squeeze(0)
        # output = [batch size, emb dim]
        weighted = weighted.squeeze(0)
        # weighted = [batch size, emb dim * 2]

        prediction = self.fc_out(torch.cat((output, weighted, embedded), dim=1))
        # prediction = [batch size, output dim]

        return prediction, hidden.squeeze(0)


class Seq2Seq(nn.Module):
    def __init__(self, encoder, decoder, device, max_seq_length=20, sos=-1, eos=-1):
        super().__init__()

        self.encoder = encoder
        self.decoder = decoder
        self.device = device
        self.max_seq_length = max_seq_length
        self.sos = sos
        self.eos = eos

        assert encoder.hid_dim == decoder.hid_dim, \
            "Hidden dimensions of encoder and decoder must be equal!"
        assert encoder.n_layers == decoder.n_layers, \
            "Encoder and decoder must have equal number of layers!"

    def forward(self, src, trg, teacher_forcing_ratio=0.5):
        # src = [src len, batch size]
        # trg = [trg len, batch size]
        # teacher_forcing_ratio is probability to use teacher forcing
        # e.g. if teacher_forcing_ratio is 0.75 we use ground-truth inputs 75% of the time

        batch_size = src.shape[1]
        trg_vocab_size = self.decoder.output_dim
        # last hidden state of the encoder is used as the initial hidden state of the decoder
        # this is considered as the context vector
        enc_outputs, hidden = self.encoder(src)

        # if we are still training the model
        if teacher_forcing_ratio != 0:
            trg_len = trg.shape[0]

            # tensor to store decoder outputs
            # because we are still training the model, we get the length from the ground truth
            outputs = torch.zeros(trg_len, batch_size, trg_vocab_size).to(self.device)

            # first input to the decoder is the <sos> tokens
            input = trg[0, :]

            for t in range(1, trg_len):
                # insert input token embedding, previous hidden and previous cell states
                # receive output tensor (predictions) and new hidden and cell states
                output, hidden = self.decoder(input, enc_outputs, hidden)

                # place predictions in a tensor holding predictions for each token
                outputs[t] = output

                # decide if we are going to use teacher forcing or not
                teacher_force = random.random() < teacher_forcing_ratio

                # get the highest predicted token from our predictions
                top1 = output.argmax(1)

                # if teacher forcing, use actual next token as next input
                # if not, use predicted token
                input = trg[t] if teacher_force else top1
        else:
            outputs = torch.zeros(self.max_seq_length, batch_size, trg_vocab_size).to(self.device)
            # first input to the decoder is the <sos> tokens
            input = torch.tensor([self.sos]).to(self.device)
            counter = 0
            # note this while loop does not properly recognize eos token
            while input.item() != self.eos and counter < self.max_seq_length:
                # insert input token, previous hidden and previous cell states
                # receive output tensor (predictions) and new hidden and cell states
                output, hidden = self.decoder(input, enc_outputs, hidden)

                softy = nn.Softmax(dim=1)
                output_probs = softy(output)

                # place predictions in a tensor holding predictions for each token
                outputs[counter] = output_probs
                # get the highest predicted token from our predictions
                top1 = output_probs.argmax(1)
                # if teacher forcing, use actual next token as next input
                # if not, use predicted token
                input = top1
                counter += 1

        return outputs


class Seq2SeqBeam(nn.Module):
    def __init__(self, encoder, decoder, device, max_seq_length=50, beam_size=2, sos=-1, eos=-1):
        super().__init__()

        self.encoder = encoder
        self.decoder = decoder
        self.device = device
        self.max_seq_length = max_seq_length
        self.beam_size = beam_size
        self.sos = sos
        self.eos = eos

        assert encoder.hid_dim == decoder.hid_dim, \
            "Hidden dimensions of encoder and decoder must be equal!"
        assert encoder.n_layers == decoder.n_layers, \
            "Encoder and decoder must have equal number of layers!"

    def forward(self, src, trg, teacher_forcing_ratio=0.5):
        # src = [src len, batch size]
        # trg = [trg len, batch size]
        # teacher_forcing_ratio is probability to use teacher forcing
        # e.g. if teacher_forcing_ratio is 0.75 we use ground-truth inputs 75% of the time
        batch_size = src.shape[1]
        trg_vocab_size = self.decoder.output_dim
        # last hidden state of the encoder is used as the initial hidden state of the decoder
        # this is considered as the context vector
        enc_outputs, hidden = self.encoder(src)

        # if we are still training the model
        if teacher_forcing_ratio != 0:
            trg_len = trg.shape[0]

            # tensor to store decoder outputs
            # because we are still training the model, we get the length from the ground truth
            outputs = torch.zeros(trg_len, batch_size, trg_vocab_size).to(self.device)

            # first input to the decoder is the <sos> tokens
            input = trg[0, :]
            # input = torch.tensor([self.sos]).to(self.device)

            for t in range(1, trg_len):
                # insert input token embedding, previous hidden and previous cell states
                # receive output tensor (predictions) and new hidden and cell states
                output, hidden = self.decoder(input, enc_outputs, hidden)

                # place predictions in a tensor holding predictions for each token
                outputs[t] = output

                # decide if we are going to use teacher forcing or not
                teacher_force = random.random() < teacher_forcing_ratio

                # get the highest predicted token from our predictions
                top1 = output.argmax(1)

                # if teacher forcing, use actual next token as next input
                # if not, use predicted token
                input = trg[t] if teacher_force else top1
            return outputs
        else:
            # this is beam decode
            # note: hidden, cell are the context vectors to consider as initial input
            softy = nn.Softmax(dim=1)

            path = []
            complete_paths = []
            state = (self.sos, hidden, 0, path)
            frontier = [state]

            beam_width = self.beam_size
            counter = 0

            while beam_width > 0 and counter < self.max_seq_length:
                extended_frontier = []
                for state in frontier:
                    input, hidden, running_prob, path = state
                    input = torch.tensor([input]).to(self.device)
                    y, hidden = self.decoder(input, enc_outputs, hidden)
                    new_probs = softy(y).squeeze(0)
                    worst_prob = 1
                    worst_idx = -1
                    for i, prob in enumerate(new_probs):
                        new_path = path + [i]
                        if prob == 0:
                            continue
                        new_prob = running_prob + math.log2(prob)
                        successor = (i, hidden, new_prob, new_path)
                        # function ADDTOBEAM

                        if len(extended_frontier) < beam_width:
                            extended_frontier.append(successor)
                            # once the extended frontier is full, need to establish the worst position
                            if len(extended_frontier) == beam_width:
                                for i, state in enumerate(extended_frontier):
                                    idx, h, p, p_path = state
                                    if p < worst_prob:
                                        worst_prob = p
                                        worst_idx = i
                        elif new_prob > worst_prob:
                            extended_frontier[worst_idx] = successor

                            # once we replace the worst state, need to re establish what the new worst state is
                            worst_prob = new_prob
                            for i, state in enumerate(extended_frontier):
                                idx, h, p, p_path = state
                                if p < worst_prob:
                                    worst_prob = p
                                    worst_idx = i

                copy_idxs = []
                for i, state in enumerate(extended_frontier):
                    # check to see if state is complete, which means ends at eos token
                    idx, h, prob, path = state
                    if idx == self.eos:
                        complete_paths.append((path, prob))
                        beam_width -= 1
                    else:
                        copy_idxs.append(i)
                temp_frontier = []
                for i in copy_idxs:
                    temp_frontier.append(extended_frontier[i])
                frontier = temp_frontier
                counter += 1

            # we now have the complete paths variable that contains tuples of (path, prob)
            # we are going to pick the max prob path and return that
            if len(complete_paths) == 0:
                probs = np.asarray([p for i, h, p, pa in frontier])
                paths = [pa for i, h, p, pa in frontier]
                idx = probs.argmax()
                return paths[idx]
            else:
                probs = np.asarray([p for pa, p in complete_paths])
                paths = [pa for pa, p in complete_paths]
                idx = probs.argmax()
                return paths[idx]


class Trainer(object):
    """
    Trainer for training a multi-class classification model
    """

    def __init__(self, model, optimizer, loss_fn, device="cpu", log_every_n=None):
        self.model = model.to(device)
        self.optimizer = optimizer
        self.device = device
        self.loss_fn = loss_fn

        self.log_every_n = log_every_n if log_every_n else 0

    def _print_summary(self):
        print(self.model)
        print(self.optimizer)
        print(self.loss_fn)

    def train(self, loader):
        """
        Run a single epoch of training
        """

        self.model.train()  # Run model in training mode

        loss_history = []
        running_loss = 0.
        running_loss_history = []

        for i, batch in enumerate(tqdm(loader)):
            X = batch.message.to(self.device)
            y = batch.reply.to(self.device)
            # print(f'Train X_shape: {X.shape}')
            # print(f'Train y_shape: {y.shape}')

            self.optimizer.zero_grad()  # Always set gradient to 0 before computing it

            logits = self.model(X, y)  # __call__ model() in this case: __call__ internally calls forward()
            # [batch_size, num_classes]

            # y = y.type_as(logits)

            logits_dim = logits.shape[-1]

            logits = logits[1:].view(-1, logits_dim)
            y = y[1:].view(-1)

            loss = self.loss_fn(logits, y)  # Compute loss: Cross entropy loss

            loss_history.append(loss.item())

            running_loss += (loss_history[-1] - running_loss) / (i + 1)  # Compute rolling average

            if self.log_every_n and i % self.log_every_n == 0:
                print("Running loss: ", running_loss)

            running_loss_history.append(running_loss)

            loss.backward()  # Perform backprop, which will compute dL/dw

            nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)  # We clip gradient's norm to 3

            self.optimizer.step()  # Update step: w = w - eta * dL / dW : eta = 1e-2 (0.01), gradient = 5e30; update value of 5e28

        print("Epoch completed!")
        print("Epoch Loss: ", running_loss)
        print("Epoch Perplexity: ", math.exp(running_loss))

        # The history information can allow us to draw a loss plot
        return loss_history, running_loss_history

    def evaluate(self, loader):
        """
        Evaluate the model on a validation set
        """

        self.model.eval()  # Run model in eval mode (disables dropout layer)

        loss_history = []
        running_loss = 0.
        running_loss_history = []

        with torch.no_grad():  # Disable gradient computation - required only during training
            for i, batch in tqdm(enumerate(loader)):
                X = batch.message.to(self.device)
                y = batch.reply.to(self.device)
                # batch[0] shape: (batch_size, input_size)

                logits = self.model(X, y, teacher_forcing_ratio=0.001)  # Run forward pass (except we don't store gradients)
                # logits shape: (batch_size, num_classes)

                # y = y.type_as(logits)

                logits_dim = logits.shape[-1]

                logits = logits[1:].view(-1, logits_dim)
                y = y[1:].view(-1)

                loss = self.loss_fn(logits, y)  # Compute loss
                # No backprop is done during validation

                loss_history.append(loss.item())

                running_loss += (loss_history[-1] - running_loss) / (i + 1)  # Compute rolling average

                running_loss_history.append(running_loss)

        return loss_history, running_loss_history

    def predict(self, sentence):
        self.model.eval()

        with torch.no_grad():  # Disable gradient computation - required only during training
            X = sentence.to(self.device)

            logits = self.model(X, X, teacher_forcing_ratio=0)  # Run forward pass (except we don't store gradients)

        return logits

    def predict_raw(self, message):
        """
        Evaluate the model on a validation set
        """

        self.model.eval()  # Run model in eval mode (disables dropout layer)

        batch_wise_predictions = []

        with torch.no_grad():  # Disable gradient computation - required only during training
            for word in message:
                X = word.to(self.device)
                # print(f'Predict X_shape: {X.shape}')

                logits = self.model(X, X, teacher_forcing_ratio=0)  # Run forward pass (except we don't store gradients)
                # logits shape: (batch_size, num_classes)

                batch_wise_predictions.append(logits)

        return batch_wise_predictions

    def get_model_dict(self):
        return self.model.state_dict()

    def run_training(self, train_loader, valid_loader, n_epochs=10):
        # Useful for us to review what experiment we're running
        # Normally, you'd want to save this to a file
        self._print_summary()

        train_losses = []
        train_running_losses = []

        valid_losses = []
        valid_running_losses = []

        for i in range(n_epochs):
            loss_history, running_loss_history = self.train(train_loader)
            # valid_loss_history, valid_running_loss_history = self.evaluate(valid_loader)

            train_losses.append(loss_history)
            train_running_losses.append(running_loss_history)

            # valid_losses.append(valid_loss_history)
            # valid_running_losses.append(valid_running_loss_history)

        # Training done, let's look at the loss curves
        all_train_losses = list(chain.from_iterable(train_losses))
        all_train_running_losses = list(chain.from_iterable(train_running_losses))

        # all_valid_losses = list(chain.from_iterable(valid_losses))
        # all_valid_running_losses = list(chain.from_iterable(valid_running_losses))

        train_epoch_idx = range(len(all_train_losses))
        # valid_epoch_idx = range(len(all_valid_losses))
        # sns.lineplot(epoch_idx, all_losses)
        sns.lineplot(train_epoch_idx, all_train_running_losses)
        # sns.lineplot(valid_epoch_idx, all_valid_running_losses)
        # plt.show()

    def run_prediction(self, train_loader, test_loader, n_epochs=10):
        self._print_summary()

        train_losses = []
        train_running_losses = []

        for i in range(n_epochs):
            loss_history, running_loss_history = self.train(train_loader)

            train_losses.append(loss_history)
            train_running_losses.append(running_loss_history)

        return self.predict(test_loader)


def init_weights(m):
    for name, param in m.named_parameters():
        nn.init.normal_(param.data, -0.08, 0.08)


def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)

def vectorize_input(sent, field):
    nlp = spacy.load('en_core_web_sm')
    tokens = [token.text.lower() for token in nlp(sent)]

    numericalized_tokens = [field.vocab.stoi[t] for t in tokens]
    unk_idx = field.vocab.stoi[field.unk_token]
    unks = [t for t, n in zip(tokens, numericalized_tokens) if n == unk_idx]

    token_tensor = torch.LongTensor(numericalized_tokens)
    token_tensor = token_tensor.unsqueeze(-1)

    return token_tensor, unks

def decode_prediction(pred, field):
    predicted_sent = []
    max_preds = pred.argmax(-1).squeeze(-1)
    for i, pred in enumerate(max_preds):
        predicted_sent.append(field.vocab.itos[pred.item()])

    string = ''
    word = predicted_sent[0]
    count = 1
    while word != '<eos>' and count < len(predicted_sent):
        string += word + ' '
        word = predicted_sent[count]
        count += 1

    string = string[:-1]

    return string

def decode_prediction_beam(pred, field):
    predicted_sent = []
    for i, p in enumerate(pred):
        predicted_sent.append(field.vocab.itos[p])

    string = ''
    word = predicted_sent[0]
    count = 1
    while word != '<eos>' and count < len(predicted_sent):
        string += word + ' '
        word = predicted_sent[count]
        count += 1

    string = string[:-1]

    return string



