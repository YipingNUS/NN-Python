import time
import numpy as np
import numpy.random as npr
import theano
import theano.tensor as T
from theano.ifelse import ifelse
from load_data import load_udm, load_udm_ss, load_mnist
from PeaNet import PeaNet
from GenNet import GenNet, projected_moments
from GCPair import GCPair
from NetLayers import relu_actfun, softplus_actfun
import utils as utils

# Simple test code, to check that everything is basically functional.
print("TESTING...")

# Initialize a source of randomness
rng = np.random.RandomState(1234)

# Load some data to train/validate/test with
dataset = 'data/mnist.pkl.gz'
datasets = load_udm(dataset, zero_mean=False)
Xtr = datasets[0][0]
tr_samples = Xtr.get_value(borrow=True).shape[0]
data_dim = Xtr.get_value(borrow=True).shape[1]
mm_proj_dim = 250

# Do moment matching in some transformed space
#P = np.identity(data_dim)
P = npr.randn(data_dim, mm_proj_dim) / np.sqrt(float(mm_proj_dim))
P = theano.shared(value=P.astype(theano.config.floatX), name='P_proj')

target_mean, target_cov = projected_moments(Xtr, P, ary_type='theano')
P = P.get_value(borrow=False).astype(theano.config.floatX)

###########################
# Setup generator network #
###########################

# Choose some parameters for the generative network
gn_params = {}
gn_config = [200, 800, 800, 28*28]
gn_params['mlp_config'] = gn_config
gn_params['lam_l2a'] = 1e-3
gn_params['vis_drop'] = 0.0
gn_params['hid_drop'] = 0.0
gn_params['bias_noise'] = 0.1
gn_params['out_noise'] = 0.1
gn_params['activation'] = softplus_actfun

# Symbolic input matrix to generator network
Xp_sym = T.matrix(name='Xp_sym')
Xd_sym = T.matrix(name='Xd_sym')

# Initialize a generator network object
GN = GenNet(rng=rng, Xp=Xp_sym, prior_sigma=5.0, params=gn_params)

###############################
# Setup discriminator network #
###############################

# Set some reasonable mlp parameters
dn_params = {}
# Set up some proto-networks
pc0 = [28*28, (200, 4), (200, 4), 11]
dn_params['proto_configs'] = [pc0]
# Set up some spawn networks
sc0 = {'proto_key': 0, 'input_noise': 0.1, 'bias_noise': 0.1, 'do_dropout': True}
#sc1 = {'proto_key': 0, 'input_noise': 0.1, 'bias_noise': 0.1, 'do_dropout': True}
dn_params['spawn_configs'] = [sc0]
dn_params['spawn_weights'] = [1.0]
# Set remaining params
dn_params['ear_type'] = 2
dn_params['ear_lam'] = 0.0
dn_params['lam_l2a'] = 1e-3
dn_params['vis_drop'] = 0.2
dn_params['hid_drop'] = 0.5
dn_params['reg_all_obs'] = False

# Initialize a discriminator network object
DN = PeaNet(rng=rng, input=T.vertical_stack(Xd_sym, GN.output), params=dn_params)

########################################################################
# Initialize the joint controller for the generator/discriminator pair #
########################################################################

gcp_params = {}
gcp_params['d_net'] = DN
gcp_params['g_net'] = GN
gcp_params['lam_l2d'] = 1e-2
gcp_params['mom_mix_rate'] = 0.03
gcp_params['mom_match_weight'] = 0.05
gcp_params['mom_match_proj'] = P
gcp_params['target_mean'] = target_mean
gcp_params['target_cov'] = target_cov

# Initialize a GCPair instance using the previously constructed generator and
# discriminator networks.
GCP = GCPair(rng=rng, d_net=DN, g_net=GN, data_dim=28*28, data_var=Xd_sym, params=gcp_params)

gn_learn_rate = 0.04
dn_learn_rate = 0.02
GCP.set_gn_sgd_params(learn_rate=gn_learn_rate, momentum=0.8)
GCP.set_dn_sgd_params(learn_rate=dn_learn_rate, momentum=0.8)
# Init generator's mean and covariance estimates with many samples
GCP.init_moments(10000)

batch_idx = T.lvector('batch_idx')
batch_sample = theano.function(inputs=[ batch_idx ], \
        outputs=Xtr.take(batch_idx, axis=0))

for i in range(750000):
    tr_idx = npr.randint(low=0,high=tr_samples,size=(100,)).astype(np.int32)
    Xn_np = GN.sample_from_prior(100)
    Xd_batch = batch_sample(tr_idx)
    Xd_batch = Xd_batch.astype(theano.config.floatX)
    Xn_batch = Xn_np.astype(theano.config.floatX)
    all_idx = np.arange(200)
    data_idx = all_idx[:100]
    noise_idx = all_idx[100:]
    d_weight = 0.05 * min(1.0, float(i)/30000.0)
    if (i < 20000):
        GCP.set_disc_weights(dweight_gn=0.001)
        outputs = GCP.train_gn(Xd_batch, Xn_batch, data_idx, noise_idx)
    else:
        GCP.set_disc_weights(dweight_gn=d_weight)
        outputs = GCP.train_joint(Xd_batch, Xn_batch, data_idx, noise_idx)
    mom_match_cost = 1.0 * outputs[0]
    disc_cost_gn = 1.0 * outputs[1]
    disc_cost_dn = 1.0 * outputs[2]
    if ((i+1 % 100000) == 0):
        gn_learn_rate = gn_learn_rate * 0.7
        dn_learn_rate = dn_learn_rate * 0.7
        GCP.set_gn_sgd_params(learn_rate=gn_learn_rate, momentum=0.8)
        GCP.set_dn_sgd_params(learn_rate=dn_learn_rate, momentum=0.8)
    if ((i % 1000) == 0):
        print("batch: {0:d}, mom_match_cost: {1:.4f}, disc_cost_gn: {2:.4f}, disc_cost_dn: {3:.4f}".format( \
                i, mom_match_cost, disc_cost_gn, disc_cost_dn))
    if ((i % 10000) == 0):
        file_name = "B_GN_SAMPLES_b{0:d}.png".format(i)
        Xs = GCP.sample_from_gn(200)
        utils.visualize_samples(Xs, file_name)
        file_name = "B_DN_WEIGHTS_b{0:d}.png".format(i)
        utils.visualize(GCP.DN, 0, 0, file_name)


print("TESTING COMPLETE!")





##############
# EYE BUFFER #
##############
