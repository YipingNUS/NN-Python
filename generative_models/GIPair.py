###################################################################
# Code for managing and training a generator/discriminator pair.  #
###################################################################

# basic python
import numpy as np
import numpy.random as npr
from collections import OrderedDict

# theano business
import theano
import theano.tensor as T
from theano.ifelse import ifelse
import theano.tensor.shared_randomstreams
from theano.sandbox.cuda.rng_curand import CURAND_RandomStreams

# phil's sweetness
from NetLayers import HiddenLayer, DiscLayer, relu_actfun, softplus_actfun
from GenNet import GenNet
from InfNet import InfNet
from PeaNet import PeaNet

def log_prob_bernoulli(p_true, p_approx):
    """
    Compute log probability of some binary variables with probabilities
    given by p_true, for probability estimates given by p_approx. We'll
    compute joint log probabilities over row-wise groups.
    """
    log_prob_1 = p_true * T.log(p_approx)
    log_prob_0 = (1.0 - p_true) * T.log(1.0 - p_approx)
    row_log_probs = T.sum((log_prob_1 + log_prob_0), axis=1, keepdims=True)
    return row_log_probs

def log_prob_gaussian(mu_true, mu_approx, le_sigma=1.0):
    """
    Compute log probability of some continuous variables with values given
    by mu_true, w.r.t. gaussian distributions with means given by mu_approx
    and standard deviations given by le_sigma. We assume isotropy.
    """
    ind_log_probs = -( (mu_approx - mu_true)**2.0 / (2.0 * le_sigma**2.0) )
    row_log_probs = T.sum(ind_log_probs, axis=1, keepdims=True)
    return row_log_probs

#
#
# Important symbolic variables:
#   Xd: Xd represents input at the "data variables" of the inferencer
#   Xc: Xc represents input at the "control variables" of the inferencer
#   Xm: Xm represents input at the "mask variables" of the inferencer
#
#

class GIPair(object):
    """
    Controller for training a variational autoencoder.

    The generator must be an instance of the GenNet class implemented in
    "GINets.py". The inferencer must be an instance of the InfNet class
    implemented in "InfNets.py".

    Parameters:
        rng: numpy.random.RandomState (for reproducibility)
        g_net: The GenNet instance that will serve as the base generator
        i_net: The InfNet instance that will serve as the base inferer
        data_dim: dimension of the "observable data" variables
        prior_dim: dimension of the "latent prior" variables
    """
    def __init__(self, rng=None, g_net=None, i_net=None, data_dim=None, \
            prior_dim=None, params=None):
        # Do some stuff!
        self.rng = theano.tensor.shared_randomstreams.RandomStreams( \
                rng.randint(100000))
        self.IN = i_net
        self.GN = g_net.shared_param_clone(rng=rng, Xp=self.IN.output)
        self.data_dim = data_dim
        self.prior_dim = prior_dim

        # output of the generator and input to the inferencer should both be
        # equal to self.data_dim
        assert(self.data_dim == self.GN.mlp_layers[-1].out_dim)
        assert(self.data_dim == self.IN.shared_layers[0].in_dim)
        # input of the generator and mu/sigma outputs of the inferencer should
        # both be equal to self.prior_dim
        assert(self.prior_dim == self.GN.mlp_layers[0].in_dim)
        assert(self.prior_dim == self.IN.mu_layers[-1].out_dim)
        assert(self.prior_dim == self.IN.sigma_layers[-1].out_dim)

        # create symbolic vars for feeding inputs to the inferencer
        self.Xd = self.IN.Xd
        self.Xc = self.IN.Xc
        self.Xm = self.IN.Xm

        # shared var learning rate for generator and inferencer
        zero_ary = np.zeros((1,)).astype(theano.config.floatX)
        self.lr_gn = theano.shared(value=zero_ary, name='gil_lr_gn')
        self.lr_in = theano.shared(value=zero_ary, name='gil_lr_in')
        # shared var momentum parameters for generator and inferencer
        self.mo_gn = theano.shared(value=zero_ary, name='gil_mo_gn')
        self.mo_in = theano.shared(value=zero_ary, name='gil_mo_in')
        # init parameters for controlling learning dynamics
        self.set_gn_sgd_params() # init SGD rate/momentum for GN
        self.set_in_sgd_params() # init SGD rate/momentum for IN

        ###################################
        # CONSTRUCT THE COSTS TO OPTIMIZE #
        ###################################
        self.data_nll_cost = self._construct_data_nll_cost()
        self.post_kld_cost = self._construct_post_kld_cost()
        self.act_reg_cost = (self.IN.act_reg_cost + self.GN.act_reg_cost) / \
                self.Xd.shape[0]
        self.joint_cost = self.data_nll_cost + self.post_kld_cost + \
                self.act_reg_cost

        # Grab the full set of "optimizable" parameters from the generator
        # and inferencer networks that we'll be working with.
        self.in_params = [p for p in self.IN.mlp_params]
        self.gn_params = [p for p in self.GN.mlp_params]

        # Initialize momentums for mini-batch SGD updates. All parameters need
        # to be safely nestled in their lists by now.
        self.joint_moms = OrderedDict()
        self.in_moms = OrderedDict()
        self.gn_moms = OrderedDict()
        for p in self.gn_params:
            p_mo = np.zeros(p.get_value(borrow=True).shape)
            self.gn_moms[p] = theano.shared(value=p_mo.astype(theano.config.floatX))
            self.joint_moms[p] = self.gn_moms[p]
        for p in self.in_params:
            p_mo = np.zeros(p.get_value(borrow=True).shape)
            self.in_moms[p] = theano.shared(value=p_mo.astype(theano.config.floatX))
            self.joint_moms[p] = self.in_moms[p]

        # Construct the updates for the generator and inferer networks
        self.joint_updates = OrderedDict()
        self.gn_updates = OrderedDict()
        self.in_updates = OrderedDict()
        for var in self.in_params:
            # these updates are for trainable params in the inferencer net...
            # first, get gradient of cost w.r.t. var
            var_grad = T.grad(self.joint_cost, var, \
                    consider_constant=[self.GN.dist_mean, self.GN.dist_cov])
            # get the momentum for this var
            var_mom = self.in_moms[var]
            # update the momentum for this var using its grad
            self.in_updates[var_mom] = (self.mo_in[0] * var_mom) + \
                    ((1.0 - self.mo_in[0]) * var_grad)
            self.joint_updates[var_mom] = self.in_updates[var_mom]
            # make basic update to the var
            var_new = var - (self.lr_in[0] * var_mom)
            if ((var in self.IN.clip_params) and \
                    (var in self.IN.clip_norms) and \
                    (self.IN.clip_params[var] == 1)):
                # clip the basic updated var if it is set as clippable
                clip_norm = self.IN.clip_norms[var]
                var_norms = T.sum(var_new**2.0, axis=1, keepdims=True)
                var_scale = T.clip(T.sqrt(clip_norm / var_norms), 0., 1.)
                self.in_updates[var] = var_new * var_scale
            else:
                # otherwise, just use the basic updated var
                self.in_updates[var] = var_new
            # add this var's update to the joint updates too
            self.joint_updates[var] = self.in_updates[var]
        for var in self.gn_params:
            # these updates are for trainable params in the generator net...
            # first, get gradient of cost w.r.t. var
            var_grad = T.grad(self.joint_cost, var, \
                    consider_constant=[self.GN.dist_mean, self.GN.dist_cov])
            # get the momentum for this var
            var_mom = self.gn_moms[var]
            # update the momentum for this var using its grad
            self.gn_updates[var_mom] = (self.mo_gn[0] * var_mom) + \
                    ((1.0 - self.mo_gn[0]) * var_grad)
            self.joint_updates[var_mom] = self.gn_updates[var_mom]
            # make basic update to the var
            var_new = var - (self.lr_gn[0] * var_mom)
            if ((var in self.GN.clip_params) and \
                    (var in self.GN.clip_norms) and \
                    (self.GN.clip_params[var] == 1)):
                # clip the basic updated var if it is set as clippable
                clip_norm = self.GN.clip_norms[var]
                var_norms = T.sum(var_new**2.0, axis=1, keepdims=True)
                var_scale = T.clip(T.sqrt(clip_norm / var_norms), 0., 1.)
                self.gn_updates[var] = var_new * var_scale
            else:
                # otherwise, just use the basic updated var
                self.gn_updates[var] = var_new
            # add this var's update to the joint updates too
            self.joint_updates[var] = self.gn_updates[var]

        # Construct batch-based training functions for the generator and
        # inferer networks, as well as a joint training function.
        #self.train_gn = self._construct_train_gn()
        #self.train_in = self._construct_train_in()
        self.train_joint = self._construct_train_joint()

        # Construct a function for computing the outputs of the generator
        # network for a batch of noise. Presumably, the noise will be drawn
        # from the same distribution that was used in training....
        self.sample_from_gn = self.GN.sample_from_model
        return

    def set_gn_sgd_params(self, learn_rate=0.02, momentum=0.9):
        """
        Set learning rate and momentum parameter for generator updates.
        """
        zero_ary = np.zeros((1,))
        new_lr = zero_ary + learn_rate
        self.lr_gn.set_value(new_lr.astype(theano.config.floatX))
        new_mo = zero_ary + momentum
        self.mo_gn.set_value(new_mo.astype(theano.config.floatX))
        return

    def set_in_sgd_params(self, learn_rate=0.02, momentum=0.9):
        """
        Set learning rate and momentum parameter for discriminator updates.
        """
        zero_ary = np.zeros((1,))
        new_lr = zero_ary + learn_rate
        self.lr_in.set_value(new_lr.astype(theano.config.floatX))
        new_mo = zero_ary + momentum
        self.mo_in.set_value(new_mo.astype(theano.config.floatX))
        return

    def _construct_data_nll_cost(self, prob_type='bernoulli'):
        """
        Construct the negative log-likelihood part of cost to minimize.
        """
        assert((prob_type == 'bernoulli') or (prob_type == 'gaussian'))
        if (prob_type == 'bernoulli'):
            log_prob_cost = log_prob_bernoulli(self.Xd, self.GN.output)
        else:
            log_prob_cost = log_prob_gaussian(self.Xd, self.GN.output, \
                    le_sigma=1.0)
        nll_cost = -T.sum(log_prob_cost) / self.Xd.shape[0]
        return nll_cost

    def _construct_post_kld_cost(self):
        """
        Construct the posterior KL-d from prior part of cost to minimize.
        """
        kld_cost = T.sum(self.IN.kld_cost) / self.Xd.shape[0]
        return kld_cost

    def _construct_train_joint(self):
        """
        Construct theano function to train inferencer and generator jointly.
        """
        outputs = [self.joint_cost, self.data_nll_cost, self.post_kld_cost, \
                self.act_reg_cost]
        func = theano.function(inputs=[ self.Xd, self.Xc, self.Xm ], \
                outputs=outputs, \
                updates=self.joint_updates)
        theano.printing.pydotprint(func, \
            outfile='GIPair_train_joint.png', compact=True, format='png', with_ids=False, \
            high_contrast=True, cond_highlight=None, colorCodes=None, \
            max_label_size=70, scan_graphs=False, var_with_name_simple=False, \
            print_output_file=True, assert_nb_all_strings=-1)
        return func

    def sample_gil_from_data(self, X_d, loop_iters=5):
        """
        Sample for several rounds through the I<->G loop, initialized with the
        the "data variable" samples in X_d.
        """
        data_samples = []
        prior_samples = []
        X_c = 0.0 * X_d
        X_m = 0.0 * X_d
        for i in range(loop_iters):
            # record the data samples for this iteration
            data_samples.append(1.0 * X_d)
            # sample from their inferred posteriors
            X_p = self.IN.sample_posterior(X_d, X_c, X_m)
            # record the sampled points (in the "prior space")
            prior_samples.append(1.0 * X_p)
            # get next data samples by transforming the prior-space points
            X_d = self.GN.transform_prior(X_p)
        result = {"data samples": data_samples, "prior samples": prior_samples}
        return result

def binarize_data(X):
    """
    Make a sample of bernoulli variables with probabilities given by X.
    """
    X_shape = X.shape
    probs = npr.rand(*X_shape)
    X_binary = 1.0 * (probs < X)
    return X_binary.astype(theano.config.floatX)

if __name__=="__main__":
    from load_data import load_udm, load_udm_ss, load_mnist
    import utils as utils
    
    # Initialize a source of randomness
    rng = np.random.RandomState(1234)

    # Load some data to train/validate/test with
    dataset = 'data/mnist.pkl.gz'
    datasets = load_udm(dataset, zero_mean=False)
    Xtr = datasets[0][0].get_value(borrow=False).astype(theano.config.floatX)
    tr_samples = Xtr.shape[0]

    # Construct a GenNet and an InfNet, then test constructor for GIPair.
    # Do basic testing, to make sure classes aren't completely broken.
    Xp = T.matrix('Xp_base')
    Xd = T.matrix('Xd_base')
    Xc = T.matrix('Xc_base')
    Xm = T.matrix('Xm_base')
    data_dim = Xtr.shape[1]
    prior_dim = 100
    prior_sigma = 2.0
    # Choose some parameters for the generator network
    gn_params = {}
    gn_config = [prior_dim, 1000, 1000, data_dim]
    gn_params['mlp_config'] = gn_config
    gn_params['activation'] = softplus_actfun
    gn_params['lam_l2a'] = 1e-3
    gn_params['vis_drop'] = 0.0
    gn_params['hid_drop'] = 0.0
    gn_params['bias_noise'] = 0.1
    gn_params['out_noise'] = 0.0
    # Choose some parameters for the inference network
    in_params = {}
    shared_config = [data_dim, 1000, 1000]
    top_config = [shared_config[-1], 1000, prior_dim]
    mu_config = top_config
    sigma_config = top_config
    in_params['shared_config'] = shared_config
    in_params['mu_config'] = mu_config
    in_params['sigma_config'] = sigma_config
    in_params['activation'] = relu_actfun
    in_params['lam_l2a'] = 1e-3
    in_params['vis_drop'] = 0.0
    in_params['hid_drop'] = 0.0
    in_params['bias_noise'] = 0.1
    in_params['input_noise'] = 0.0
    # Initialize the base networks for this GIPair
    IN = InfNet(rng=rng, Xd=Xd, Xc=Xc, Xm=Xm, prior_sigma=prior_sigma, \
            params=in_params, mlp_param_dicts=None)
    GN = GenNet(rng=rng, Xp=Xp, prior_sigma=prior_sigma, \
            params=gn_params, mlp_param_dicts=None)
    # Initialize biases in IN and GN
    IN.init_biases(0.1)
    GN.init_biases(0.1)
    # Initialize the GIPair
    GIP = GIPair(rng=rng, g_net=GN, i_net=IN, data_dim=data_dim, \
            prior_dim=prior_dim, params=None)
    # Set initial learning rate and basic SGD hyper parameters
    in_learn_rate = 0.005
    gn_learn_rate = 0.005
    GIP.set_in_sgd_params(learn_rate=in_learn_rate, momentum=0.8)
    GIP.set_gn_sgd_params(learn_rate=gn_learn_rate, momentum=0.8)

    for i in range(750000):
        # get some data to train with
        tr_idx = npr.randint(low=0,high=tr_samples,size=(100,))
        Xd_batch = binarize_data(Xtr.take(tr_idx, axis=0))
        Xc_batch = 0.0 * Xd_batch
        Xm_batch = 0.0 * Xd_batch
        # do a minibatch update of the model, and compute some costs
        outputs = GIP.train_joint(Xd_batch, Xc_batch, Xm_batch)
        joint_cost = 1.0 * outputs[0]
        data_nll_cost = 1.0 * outputs[1]
        post_kld_cost = 1.0 * outputs[2]
        act_reg_cost = 1.0 * outputs[3]
        if (i < 50000):
            scale = float(i) / 50000.0
            GIP.set_in_sgd_params(learn_rate=(scale*in_learn_rate), momentum=0.8)
            GIP.set_gn_sgd_params(learn_rate=(scale*gn_learn_rate), momentum=0.8)
        if ((i+1 % 100000) == 0):
            in_learn_rate = in_learn_rate * 0.7
            gn_learn_rate = gn_learn_rate * 0.7
            GIP.set_in_sgd_params(learn_rate=in_learn_rate, momentum=0.9)
            GIP.set_gn_sgd_params(learn_rate=gn_learn_rate, momentum=0.9)
        if ((i % 1000) == 0):
            print("batch: {0:d}, joint_cost: {1:.4f}, data_nll_cost: {2:.4f}, post_kld_cost: {3:.4f}, act_reg_cost: {4:.4f}".format( \
                    i, joint_cost, data_nll_cost, post_kld_cost, act_reg_cost))
        if ((i % 10000) == 0):
            file_name = "GN_SAMPLES_b{0:d}.png".format(i)
            Xs = GN.sample_from_model(200)
            utils.visualize_samples(Xs, file_name)

    print("TESTING COMPLETE!")




##############
# EYE BUFFER #
##############
