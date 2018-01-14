import numpy as np
import theano
import theano.tensor as T
from lasagne.updates import norm_constraint


class SGVBWords(object):
    """ Implementation of the Stochastic Gradient Variational Bayes algorithm for VAEs
    """

    def __init__(self, generative_model, recognition_model, z_dim, max_length, vocab_size, embedding_dim, dist_z_gen,
                 dist_x_gen, dist_z_rec, gen_nn_kwargs, rec_nn_kwargs, eos_ind):
        """

        :param generative_model:        # class of the generative model
        :param recognition_model:       # class of the recognition model
        :param z_dim:                   # dimension of the latent variable (Z)
        :param max_length:              # actual maximum length of any sentence (L)
        :param vocab_size:              # number of distinct tokens in vocabulary (V + 1)
        :param embedding_dim:           # size of the embedding (E)
        :param dist_z_gen:              # distribution for the latents in the generative model
        :param dist_x_gen:              # distribution for the observed in the generative model
        :param dist_z_rec:              # distribution for the latents in the recognition model
        :param gen_nn_kwargs:           # params for the generative model
        :param rec_nn_kwargs:           # params for the recognition model
        :param eos_ind:                 # index of the EOS token
        """

        self.z_dim = z_dim
        self.max_length = max_length
        self.vocab_size = vocab_size
        self.embedding_dim = embedding_dim

        # Creates a V x E embedding matrix of samples from the Normal distribution with 0 Mean and 0.1 StD
        # This is an updatable variable (theano concept of "current value" and "update")
        self.all_embeddings = theano.shared(np.float32(np.random.normal(0., 0.1, (vocab_size, embedding_dim))))
        # ((V + 1) x E) initial embedding parameters

        self.dist_z_gen = dist_z_gen
        self.dist_x_gen = dist_x_gen
        self.dist_z_rec = dist_z_rec

        self.gen_nn_kwargs = gen_nn_kwargs
        self.rec_nn_kwargs = rec_nn_kwargs

        self.generative_model = self.init_generative_model(generative_model)        # initialise generative model
        self.recognition_model = self.init_recognition_model(recognition_model)     # initialise recognition model

        self.eos_ind = eos_ind

    def init_generative_model(self, generative_model):
        """ Initialise the generative model

        :param generative_model:    class of the generative model to initialise

        :return:                    initialised generative model
        """

        return generative_model(self.z_dim, self.max_length, self.vocab_size, self.embedding_dim, self.embedder,
                                self.dist_z_gen, self.dist_x_gen, self.gen_nn_kwargs)

    def init_recognition_model(self, recognition_model):
        """ Initialise the recognition model

        :param recognition_model:   class of the recognition model to initialise

        :return:                    initialised recognition model
        """

        return recognition_model(self.z_dim, self.max_length, self.embedding_dim, self.dist_z_rec, self.rec_nn_kwargs)

    def embedder(self, x, all_embeddings):
        """ Embed a sentence using the current embeddings

        :param x:                   max(L) -dimensional vector representing a sentence
        :param all_embeddings:      ((V + 1) x E) matrix for the embedding to use

        :return:                    (max(L) x E) tensor representing the embedded sentence
        """

        # Embedding are parameters of the model, we learn them over time (here we just do 0-padding)
        all_embeddings = T.concatenate([all_embeddings, T.zeros((1, self.embedding_dim))], axis=0)

        # Returns the embedding for the sentence
        return all_embeddings[x]

    def cut_off(self, x):
        """

        :param x:

        :return:
        """

        def step(x_l, x_lm1):

            x_l = T.switch(T.eq(x_lm1, self.eos_ind), -1, x_l)
            x_l = T.switch(T.eq(x_lm1, -1), -1, x_l)

            return T.cast(x_l, 'int32')

        x_cut_off, _ = theano.scan(step,
                                   sequences=x.T,
                                   outputs_info=T.zeros((x.shape[0],), 'int32'),
                                   )

        return x_cut_off.T

    def symbolic_elbo(self, x, x_m, num_samples, optimal_ratio=False, beta=None, drop_mask=None):
        """

        :param x:               (N x max(L)) matrix
        :param x_m:             (N x max(L)) matrix
        :param num_samples:     int (S)
        :param optimal_ratio:   boolean
        :param beta:            scalar
        :param drop_mask:       (N x max(L)) matrix

        :return:                scalar and scalar and scalar
        """

        x_embedded = self.embedder(x, self.all_embeddings)                                              # N x max(L) x E
        x_m_embedded = self.embedder(x_m, self.all_embeddings)                                          # N x max(L) x E

        z, kl = self.recognition_model.get_samples_and_kl_std_gaussian(x_m, x_m_embedded, num_samples)  # ((S * N) x Z) and (N x 1)

        if drop_mask is None:
            x_embedded_dropped = x_embedded                                                             # N x max(L) x E
        else:
            x_embedded_dropped = x_embedded * T.shape_padright(drop_mask)                               # N x max(L) x E


        log_p_x = self.generative_model.log_p_x(x, x_embedded, x_embedded_dropped, z, self.all_embeddings)  # S x N

        if optimal_ratio:
            elbo = T.sum((1. / num_samples) * log_p_x) - T.sum(kl)/2
        else:
            if beta is None:
                elbo = T.sum((1. / num_samples) * log_p_x) - T.sum(kl)
            else:
                elbo = T.sum((1. / num_samples) * log_p_x) - T.sum(beta * kl)

        pp = T.exp(-(T.sum((1. / num_samples) * log_p_x) - T.sum(kl)) / T.sum(T.switch(T.lt(x, 0), 0, 1)))

        return elbo, T.sum(kl), pp

    def elbo_fn(self, num_samples):
        """

        :param num_samples:         scalar

        :return:                    ...
        """

        x = T.imatrix('x')                                                                              # N x max(L)
        x_m = T.imatrix('x_m')                                                                          # N x max(L)

        elbo, kl, pp = self.symbolic_elbo(x, x_m, num_samples, beta=None, drop_mask=None, optimal_ratio=False)

        elbo_fn = theano.function(inputs=[x, x_m],
                                  outputs=[elbo, kl, pp],
                                  allow_input_downcast=True,
                                  on_unused_input='ignore',
                                  )

        return elbo_fn

    def optimiser(self, num_samples, grad_norm_constraint, update, update_kwargs, optimal_ratio, saved_update=None):
        """

        :param num_samples:             scalar
        :param grad_norm_constraint:    ...
        :param update:                  ...
        :param update_kwargs:           ...
        :param optimal_ratio:           ...
        :param saved_update:            ...

        :return:
        """

        x = T.imatrix('x')                                                                              # N x max(L)
        x_m = T.imatrix('x_m')                                                                          # N x max(L)
        beta = T.scalar('beta')                                                                         # scalar
        drop_mask = T.matrix('drop_mask')                                                               # N x max(L)

        elbo, kl, pp = self.symbolic_elbo(x, x_m, num_samples, optimal_ratio, beta, drop_mask)

        params = self.generative_model.get_params() + self.recognition_model.get_params() + [self.all_embeddings]
        grads = T.grad(-elbo, params, disconnected_inputs='ignore')

        if grad_norm_constraint is not None:
            grads = [norm_constraint(g, grad_norm_constraint) if g.ndim > 1 else g for g in grads]

        update_kwargs['loss_or_grads'] = grads
        update_kwargs['params'] = params

        updates = update(**update_kwargs)

        if saved_update is not None:
            for u, v in zip(updates, saved_update.keys()):
                u.set_value(v.get_value())

        optimiser = theano.function(inputs=[x, x_m, beta, drop_mask],
                                    outputs=[elbo, kl, pp],
                                    updates=updates,
                                    allow_input_downcast=True,
                                    on_unused_input='ignore',
                                    )

        return optimiser, updates

    def generate_output_prior_fn(self, num_samples, beam_size, num_time_steps=None):
        """

        :param num_samples:         scalar
        :param beam_size:           ...
        :param num_time_steps:      ...

        :return:                    ...
        """

        outputs, updates = self.generative_model.generate_output_prior(self.all_embeddings, num_samples, beam_size,
                                                                       num_time_steps)

        return theano.function(inputs=[],
                               outputs=outputs,
                               updates=updates,
                               allow_input_downcast=True,
                               )

    def generate_output_posterior_fn_generative(self, x, x_m, z, all_embeddings, beam_size, num_time_steps=None):
        """

        :param x:
        :param z:
        :param all_embeddings:
        :param beam_size:
        :param num_time_steps:

        :return:
        """

        x_gen_sampled, x_gen_argmax, updates = self.generative_model.generate_text(z, all_embeddings)

        x_gen_beam = self.generative_model.beam_search(z, all_embeddings, beam_size)

        generate_output_posterior = theano.function(inputs=[x, x_m],
                                                    outputs=[z, x_gen_sampled, x_gen_argmax, x_gen_beam],
                                                    updates=updates,
                                                    allow_input_downcast=True,
                                                    on_unused_input='ignore'
                                                    )

        return generate_output_posterior

    def generate_output_posterior_fn(self, beam_size, num_time_steps=None):
        """

        :param beam_size:
        :param num_time_steps:

        :return:
        """

        x = T.imatrix('x')                                                                              # N x max(L)
        x_m = T.imatrix('x_m')                                                                          # N x max(L)

        x_embedded = self.embedder(x, self.all_embeddings)                                              # N x max(L) x E
        x_m_embedded = self.embedder(x_m, self.all_embeddings)                                          # N x max(L) x E

        z = self.recognition_model.get_samples(x_m, x_m_embedded, 1, means_only=True)                   # N x Z

        return self.generate_output_posterior_fn_generative(x, x_m, z, self.all_embeddings, beam_size, num_time_steps)

    def generate_canvases_prior_fn(self, num_samples, beam_size):
        """

        :param num_samples:
        :param beam_size:

        :return:
        """

        time_steps = self.generative_model.nn_canvas_rnn_time_steps

        z = T.matrix('z')                                                                               # S x Z

        fns = []

        for t in range(time_steps):

            fns.append(self.generative_model.generate_canvas_prior_fn(z, self.all_embeddings, beam_size, t+1))

        return fns

    def impute_missing_words_fn(self, beam_size):
        """

        :param beam_size:

        :return:
        """

        x_best_guess = T.imatrix('x_best_guess')                                                        # N x max(L)
        missing_words_mask = T.matrix('drop_mask')                                                      # N x max(L)

        x_best_guess_embedded = self.embedder(x_best_guess, self.all_embeddings)                        # N x max(L) x E

        z = self.recognition_model.get_samples(x_best_guess, x_best_guess_embedded, 1, means_only=True) # N x Z

        x_best_guess_new = self.generative_model.impute_missing_words(z, x_best_guess, missing_words_mask,
                                                                      self.all_embeddings, beam_size)   # N x max(L)

        return theano.function(inputs=[x_best_guess, missing_words_mask],
                               outputs=x_best_guess_new,
                               allow_input_downcast=True,
                               )

    def follow_latent_trajectory_fn(self, num_samples, beam_size):
        """

        :param num_samples:
        :param beam_size:

        :return:
        """

        alphas = T.vector('alphas')

        return self.generative_model.follow_latent_trajectory_fn(self.all_embeddings, alphas, num_samples, beam_size)

    def find_best_matches_fn(self):
        """

        :return:
        """

        sentences_in = T.imatrix('sentences_in')                                                        # S x max(L)
        sentences_eval = T.imatrix('sentences_eval')                                                    # N x max(L)

        sentences_in_embedded = self.embedder(sentences_in, self.all_embeddings)                        # S x max(L) x E
        sentences_eval_embedded = self.embedder(sentences_eval, self.all_embeddings)                    # N x max(L) x E

        z = self.recognition_model.get_samples(sentences_in, sentences_in_embedded, 1, means_only=True) # S x Z

        return self.generative_model.find_best_matches_fn(sentences_in, sentences_eval, sentences_eval_embedded, z,
                                                          self.all_embeddings)
