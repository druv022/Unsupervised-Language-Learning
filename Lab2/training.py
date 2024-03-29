from data import TokenizedCorpus, Vocabulary
from embedalign import FFNN, LSTM, ELBO, ApproxBiLSTM
import torch
from torch.nn import Softplus, Embedding
import torch.optim as optim
from torch.distributions.multivariate_normal import MultivariateNormal
import time
import pickle
import copy
import numpy as np

torch.manual_seed(1)


class Training:

    def __init__(self, data_loc, epochs=30, batch_size = 32, dim_z=32, embedding_dim=128, hidden_dim=100, read=0):
        '''
        :param data_loc: list of location of L1 and L2;[L1, L2]
        :param epochs: number of epoch
        :param batch_size: batch size
        :param dim_z: dimension of latent variable
        :param embedding_dim: dimension of internal word embedding
        :param hidden_dim: dimension of final word embedding
        :param read: the number of lines to be read, pass 0 for all
        '''

        l1, l2 = TokenizedCorpus(data_loc[0]), TokenizedCorpus(data_loc[1])
        self.L1_sentences = l1.get_words("english", read_lines=read)
        self.L2_sentences = l2.get_words("french", read_lines=read)
        self.V1 = Vocabulary(self.L1_sentences,process_all=False)
        self.V2 = Vocabulary(self.L2_sentences,process_all=False)
        self.sentence_length = 64     # max number of tokens in a sentence
        self.L1_data = self.tokenize_data(self.L1_sentences, self.V1)
        self.L2_data = self.tokenize_data(self.L2_sentences, self.V2)

        self.epochs = epochs
        self.batch_size = batch_size
        self.dim_Z = dim_z
        self.embedding_dim = embedding_dim
        self.hidden_dim = hidden_dim
        pad = self.V1.w2i_f["<pad>"]

        # Networks
        self.lstm = LSTM(len(self.V1.w2i_f), hidden_dim, embedding_dim, pad, bidirectn_flag=True, batch_first=True, batch_size=self.batch_size)
        self.ffnn1 = FFNN(self.dim_Z, 250, len(self.V1.w2i_f))
        self.ffnn2 = FFNN(self.dim_Z, 250, len(self.V2.w2i_f))
        self.ffnn3 = FFNN(hidden_dim, self.dim_Z, hidden_size=250, hidden_layer=False) # when using LSTM
        self.ffnn4 = FFNN(hidden_dim, self.dim_Z, hidden_size=250, hidden_layer=False) # when using LSTM

        # This is not tested thoroughly
        # self.ffnn3 = FFNN(embedding_dim*2, self.dim_Z, hidden_size=250, hidden_layer= False)  # when using approx LSTM
        # self.ffnn4 = FFNN(embedding_dim*2, self.dim_Z, hidden_size=250, hidden_layer= False)  # when using approx LSTM
        # self.approxbi = ApproxBiLSTM(len(self.V1.w2i_f), embedding_dim, pad, batch_size=self.batch_size)
        # when using Approx LSTM
        # params = list(self.ffnn1.parameters()) + list(self.ffnn2.parameters()) + list(self.ffnn3.parameters()) \
        #          + list(self.ffnn4.parameters()) + list(self.approxbi.word_embeddings.parameters())

        # when using LSTM
        params = list(self.ffnn1.parameters()) + list(self.ffnn2.parameters()) + list(self.ffnn3.parameters()) \
                 + list(self.ffnn4.parameters()) + list(self.lstm.parameters())

        self.opt = optim.Adam(params)

    # This method tokenize sentences based on the type of vocabulary object(language specific)
    def tokenize_sentence(self, sentence, V):
        sentence_t = []

        for i, word in enumerate(sentence):
            if word in V.w2i:
                sentence_t.append(V.get_index(word))
            else:
                sentence_t.append(V.w2i_f["<unk>"])

        len_sentence_t = len(sentence_t)
        if len_sentence_t < self.sentence_length:
            diff = self.sentence_length - len_sentence_t
            pad = [V.w2i_f["<pad>"]] * diff
            sentence_t = sentence_t + pad
        else:
            sentence_t = sentence_t[0:self.sentence_length]

        return sentence_t

    # This method tokenize the whole dataset by calling tokenize_sentence on each sentence
    def tokenize_data(self, sentences, V):
        l_data = []
        for sentence in sentences:
            l_data.append(self.tokenize_sentence(sentence, V))

        return torch.Tensor(l_data).long()

    # Returns a minibatch
    def minibatch(self):

        for i in range(0, len(self.L1_data), self.batch_size):
            yield [self.L1_data[i:i + self.batch_size], self.L2_data[i:i + self.batch_size]]

    # Trains the model
    def train(self):
        print("-------------Training---------------")
        print("-------------------------------------")

        prev_loss = 0
        for epoch in range(self.epochs):
            print("*****************EPOCH ", epoch, "**************************")
            updates = 0
            training_loss = 0
            start = time.time()
            multivariate_n = MultivariateNormal(torch.zeros(self.dim_Z), torch.eye(self.dim_Z))
            for L_batch in self.minibatch():
                updates += 1
                L1_batch = L_batch[0]
                L2_batch = L_batch[1]

                mask_l1 = torch.Tensor(np.where(L1_batch > 0,1,0))
                mask_l2 = torch.Tensor(np.where(L2_batch > 0, 1, 0))

                # This check is required because the LSTM network depends on fixed batch size
                if L1_batch.shape[0] != self.batch_size:
                    continue

                h_1 = self.lstm(L1_batch)
                h = (h_1[:, :, 0:self.hidden_dim] + h_1[:, :, self.hidden_dim:]) / 2

                # h_1 = self.approxbi.getEmbedding(L1_batch)
                # h = h_1

                mu_h = self.ffnn3(h, linear_activation=True)
                self.ffnn4.softmax = Softplus()
                sigma = self.ffnn4(h)

                epsilon = multivariate_n.sample((self.batch_size,self.sentence_length,))
                z = mu_h + epsilon * torch.sqrt(sigma)

                cat_x = self.ffnn1(z)
                cat_y = self.ffnn2(z)

                self.lstm.zero_grad()
                self.ffnn1.zero_grad()
                self.ffnn2.zero_grad()
                self.ffnn3.zero_grad()
                self.ffnn4.zero_grad()

                elbo_c = ELBO(self.sentence_length, self.sentence_length)
                elbo_p1 = elbo_c.elbo_p1(cat_x, L1_batch, mask_l1)
                # print(elbo_p1)
                elbo_p2 = elbo_c.elbo_p2(cat_y, L2_batch, mask_l2)
                # print(elbo_p2)
                elbo_p3 = elbo_c.elbo_p3([mu_h, sigma])
                # print(elbo_p3)

                loss = -(elbo_p1 + elbo_p2 - elbo_p3)

                training_loss += loss.data[0]
                loss.backward(retain_graph=True)
                self.opt.step()


            print("iter %r: loss=%.4f, time=%.2fs" %
                      (epoch, training_loss / updates, time.time() - start))

            mloss = training_loss / updates
            print("iter %r: loss=%.4f, time=%.2fs" %
                  (epoch, mloss, time.time() - start))
            if not prev_loss or mloss < prev_loss:
                prev_loss = mloss
                torch.save(training.ffnn1, 'ffnn1.pt')
                torch.save(training.ffnn2, 'ffnn2.pt')
                torch.save(training.ffnn3, 'ffnn3.pt')
                torch.save(training.ffnn4, 'ffnn4.pt')
                torch.save(training.lstm, 'lstm.pt')

        self.V1.save_word_indexes("L1")
        self.V2.save_word_indexes("L2")


if __name__ == "__main__":
    # L1_data = "./data/wa/dev.en"
    # L2_data = "./data/wa/dev.fr"

    L1_data = "./data/wa/dev_dup.en"
    L2_data = "./data/wa/dev_dup.fr"

    # L1_data = "./data/hansards/training.en"
    # L2_data = "./data/hansards/training.fr"

    training = Training([L1_data, L2_data], 2, batch_size=128, dim_z=150, embedding_dim=128, hidden_dim=100, read=000)
    training.train()



