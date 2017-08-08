from __future__ import print_function
from itertools import chain
import numpy as np
import tensorflow as tf
import pandas as pd
import os
slim = tf.contrib.slim
from models import lrelu,DiscriminatorW,Grad_Penalty
from label_loader import get_label_loader
from utils import summary_stats

debug=False

class CausalController(object):
    model_type='controller'
    summs=['cc_summaries']
    def summary_scalar(self,name,ten):
        if self.build_summaries:
            tf.summary.scalar(name,ten,collections=self.summs)
    def summary_stats(self,name,ten,hist=False):
        if self.build_summaries:
            summary_stats(name,ten,collections=self.summs,hist=hist)

    def load(self,sess,path):
        '''
        sess is a tf.Session object
        path is the path of the file you want to load, (not the directory)
        Example
        ./checkpoint/somemodel/saved/model.ckpt-3000
        (leave off the extensions)
        '''
        if not hasattr(self,'saver'):#should have one now
            self.saver=tf.train.Saver(var_list=self.var)
        print('Attempting to load model:',path)
        self.saver.restore(sess,path)

    #def __init__(self,graph,config,batch_size=1,input_dict=None,reuse=None):
    def __init__(self,batch_size,config):
        '''
        Args:
            config    : This carries all the aguments defined in
            causal_controller/config.py with it. It also defines config.graph,
            which is a nested list that specifies the graph

            batch_size: This is separate from config because it is actually a
            tf.placeholder so that batch_size can be set during sess.run, but
            also synchronized between the models.

        A causal graph (config.graph) is specified as follows:
            just supply a list of pairs (node, node_parents)

            Example: A->B<-C; D->E

            [ ['A',[]],
              ['B',['A','C']],
              ['C',[]],
              ['D',[]],
              ['E',['D']]
            ]

            I use a list right now instead of a dict because I don't think
            dict.keys() are gauranteed to be returned a particular order.
            TODO:A good improvement would be to use collections.OrderedDict

            #old
            #Pass indep_causal=True to use Unif[0,1] labels
            #input_dict allows the model to take in some aritrary input instead
            #of using tf_random_uniform nodes
            #pass reuse if constructing for a second time

            Access nodes ether with:
            model.cc.node_dict['Male']
            or with:
            model.cc.Male


        Other models such as began/dcgan are intended to be build more than
        once (for example on 2 gpus), but causal_controller is just built once.

        '''

        if len(tf.get_collection(self.summs[0]))==0:
            self.build_summaries=True
        else:
            self.build_summaries=False


        self.config=config
        self.batch_size=batch_size #tf.placeholder_with_default
        self.graph=config.graph
        self.node_names, self.parent_names=zip(*self.graph)
        self.node_names=list(self.node_names)


        #set nodeclass attributes
        if debug:
            print('Using ',self.config.cc_n_layers,'between each causal node')
        CausalNode.n_layers=self.config.cc_n_layers
        CausalNode.n_hidden=self.config.cc_n_hidden
        CausalNode.batch_size=self.batch_size


        with tf.variable_scope('causal_controller') as vs:
            self.step=tf.Variable(0, name='step', trainable=False)
            self.inc_step=tf.assign(self.step,self.step+1)

            self.nodes=[CausalNode(name=n,config=config) for n in self.node_names]


            #={n:CausalNode(n) for n in self.node_names}
            for node,rents in zip(self.nodes,self.parent_names):
                node.parents=[n for n in self.nodes if n.name in rents]

            ##construct graph##
            #Lazy construction avoids the pain of traversing the causal graph explicitly
            for node in self.nodes:
                node.setup_tensor()

            self.labels=tf.concat(self.list_labels(),-1)
            self.fake_labels=self.labels
            self.fake_labels_logits= tf.concat( self.list_label_logits(),-1 )

        self.label_dict={n.name:n.label for n in self.nodes}
        self.node_dict={n.name:n for n in self.nodes}
        #enable access directly. Little dangerous
            #Please don't have any nodes named "batch_size" for example
        self.__dict__.update(self.node_dict)

        #dcc variables are not saved, so if you reload in the middle of a
        #pretrain, that might be a quirk. I don't find it makes much of a
        #difference though
        self.var = tf.contrib.framework.get_variables(vs)
        trainable=tf.get_collection('trainable_variables')
        self.train_var=[v for v in self.var if v in trainable]

        #wont save dcc var
        self.saver=tf.train.Saver(var_list=self.var)
        self.model_dir=os.path.join(self.config.model_dir,self.model_type)
        self.save_model_dir=os.path.join(self.model_dir,'checkpoints')
        if not os.path.exists(self.model_dir):
            os.mkdir(self.model_dir)
        if not os.path.exists(self.save_model_dir):
            os.mkdir(self.save_model_dir)


    def build_pretrain(self,label_loader):
        '''
        This is not called if for example using an existing model
        label_loader is a queue of only labels that moves quickly because no
        images
        '''
        #Use Native queue
        config=self.config

        #Pretraining setup
        self.DCC=DiscriminatorW

        #Assuming factorized right now
        #if self.config.pt_factorized:
            #self.DCC=FactorizedNetwork(self.graph,self.DCC,self.config)

        #reasonable alternative with equal performance
        if self.config.pt_factorized:#Each node owns a dcc
            for node in self.nodes:
                node.setup_pretrain(config,label_loader,self.DCC)

            with tf.control_dependencies([self.inc_step]):
                self.c_optim=tf.group(*[n.c_optim for n in self.nodes])
            self.dcc_optim=tf.group(*[n.dcc_optim for n in self.nodes])
            self.train_op=tf.group(self.c_optim,self.dcc_optim)

            self.c_loss=tf.reduce_sum([n.c_loss for n in self.nodes])
            self.dcc_loss=tf.reduce_sum([n.dcc_loss for n in self.nodes])

            self.summary_stats('total_c_loss',self.c_loss)
            self.summary_stats('total_dcc_loss',self.dcc_loss)

        #default.
        else:#Not factorized. CC owns dcc
            print('setting up pretrain:','CausalController')
            real_inputs=tf.concat([label_loader[n] for n in self.node_names],axis=1)
            fake_inputs=self.labels
            n_hidden=self.config.critic_hidden_size
            real_prob,self.dcc_real_logit,self._dcc_var=self.DCC(real_inputs,self.batch_size,n_hidden,self.config)
            fake_prob,self.dcc_fake_logit,_=self.DCC(fake_inputs,self.batch_size,n_hidden,self.config,reuse=True)
            grad_cost,self.dcc_slopes=Grad_Penalty(real_inputs,fake_inputs,self.DCC,self.config)

            self.dcc_diff = self.dcc_fake_logit - self.dcc_real_logit
            self.dcc_gan_loss=tf.reduce_mean(self.dcc_diff)
            self.dcc_grad_loss=grad_cost
            self.dcc_loss=self.dcc_gan_loss+self.dcc_grad_loss#
            self.c_loss=-tf.reduce_mean(self.dcc_fake_logit)#

            optimizer = tf.train.AdamOptimizer
            self.c_optimizer, self.dcc_optimizer = optimizer(config.pt_cc_lr),optimizer(config.pt_dcc_lr)

            with tf.control_dependencies([self.inc_step]):
                self.c_optim=self.c_optimizer.minimize(self.c_loss,var_list=self.train_var)
            self.dcc_optim=self.dcc_optimizer.minimize(self.dcc_loss,var_list=self.dcc_var)
            self.train_op=tf.group(self.c_optim,self.dcc_optim)

            self.summary_stats('total_c_loss',self.c_loss)
            self.summary_stats('total_dcc_loss',self.dcc_loss)


            for node in self.nodes:
                with tf.name_scope(node.name):
                    #TODO:replace with summary_stats
                    self.summary_stats(node.name+'_fake',node.label,hist=True)
                    self.summary_stats(node.name+'_real',label_loader[node.name],hist=True)


        self.summaries=tf.get_collection(self.summs[0])
        print('causalcontroller has',len(self.summaries),'summaries')
        self.summary_op=tf.summary.merge(self.summaries)


    @property
    def dcc_var(self):
        if self.config.is_pretrain:
            if self.config.pt_factorized:
                return list(chain.from_iterable([n.dcc_var for n in self.nodes]))
            else:
                return self._dcc_var
        else:
            return []



    def critic_update(self,sess):
        fetch_dict = {"critic_op":self.dcc_optim }
        for i in range(self.config.n_critic):
            result = sess.run(fetch_dict)


    def __len__(self):
        return len(self.node_dict)

    @property
    def feed_z(self):#might have to makethese phw/default
        return {key:val.z for key,val in self.node_dict.iteritems()}
    @property
    def sample_z(self):
        return {key:val.z for key,val in self.node_dict.iteritems()}

    def list_placeholders(self):
        return [n.z for n in self.nodes]
    def list_labels(self):
        return [n.label for n in self.nodes]
    def list_label_logits(self):
        return [n.label_logit for n in self.nodes]



class CausalNode(object):
    '''
    A CausalNode sets up a small neural network:
    z_noise+[,other causes] -> label_logit

    Everything is defined in terms of @property
    to allow tensorflow graph to be lazily generated as called
    because I don't enforce that a node's parent tf graph
    is constructed already during class.setup_tensor

    Uniform[-1,1] + other causes pases through n_layers fully connected layers.
    '''
    train = True
    name=None
    #logit is going to be 1 dim with sigmoid
    #as opposed to 2 dim with softmax
    _label_logit=None
    _label=None
    parents=[]#list of CausalNodes
    n_layers=3
    n_hidden=10
    batch_size=-1#Must be set by cc

    summs=['cc_summaries']
    def summary_stats(self,name,ten,hist=False):
        summary_stats(name,ten,collections=self.summs,hist=hist)

    def __init__(self,name,config):
        self.name=name
        self.config=config

        if self.batch_size==-1:
            raise Exception('class attribute CausalNode.batch_size must be set')

        #Use tf.random_uniform instead of placeholder for noise
        with tf.variable_scope(self.name) as vs:
            self.z=tf.random_uniform((self.batch_size,self.n_hidden),minval=-1.0,maxval=1.0)
            self.init_var = tf.contrib.framework.get_variables(vs)
            self.setup_var=[]#empty until setup_tensor runs

    def setup_tensor(self):
        if self._label is not None:#already setup
            if debug:
                #Notify that already setup (normal behavior)
                print('self.',self.name,' has refuted setting up tensor')
            return

        tf_parents=[self.z]+[node.label for node in self.parents]


        print("Warning, using lrelu instead of tanh!")
        with tf.variable_scope(self.name) as vs:
            h=tf.concat(tf_parents,-1)#vector of parent tensors
            for l in range(self.n_layers-1):
                h=slim.fully_connected(h,self.n_hidden,activation_fn=lrelu,scope='layer'+str(l))
                #h=slim.fully_connected(h,self.n_hidden,activation_fn=tf.nn.tanh,scope='layer'+str(l))

            self._label_logit = slim.fully_connected(h,1,activation_fn=None,scope='proj')
            self._label=tf.nn.sigmoid( self._label_logit )
            if debug:
                print('self.',self.name,' has setup _label=',self._label)

            #There could actually be some (quiet) error here I think if one of the
            #names is a substring of some other name. Sorry coded poorly
            self.setup_var=tf.contrib.framework.get_variables(vs)
    @property
    def var(self):
        if len(self.setup_var)==0:
            print('WARN: node var was accessed before it was constructed')
        return self.init_var+self.setup_var
    @property
    def train_var(self):
        trainable=tf.get_collection('trainable_variables')
        return [v for v in self.var if v in trainable]

    @property
    def label_logit(self):
        #Less stable. Better to access labels
        #for input to another model
        if self._label_logit is not None:
            return self._label_logit
        else:
            self.setup_tensor()
            return self._label_logit
    @property
    def label(self):
        if self._label is not None:
            return self._label
        else:
            self.setup_tensor()
            return self._label


    def setup_pretrain(self,config,label_loader,DCC):
        '''
        This only happens if cc_config.pt_factorized=True
        In this case convergence of each node is treated like it's own gan
        conditioned on the parent nodes labels.

        '''
        print('setting up pretrain:',self.name)

        with tf.variable_scope(self.name,reuse=self.reuse) as vs:
            self.config=config
            n_hidden=self.config.critic_hidden_size

            parent_names=[p.name for p in self.parents]
            #real_inputs=tf.concat([label_loader[n] for n in parent_names]+[label_loader[self.name]],axis=1)
            #fake_inputs=tf.concat([label_loader[n] for n in parent_names]+[self.label],axis=1)
            real_inputs=tf.concat([label_loader[n] for n in parent_names]+[label_loader[self.name]],axis=1)
            fake_inputs=tf.concat([p.label for p in self.parents]+[self.label],axis=1)

            real_prob,self.dcc_real_logit,self.dcc_var=DCC(real_inputs,self.batch_size,n_hidden,self.config)
            fake_prob,self.dcc_fake_logit,_=DCC(fake_inputs,self.batch_size,n_hidden,self.config,reuse=True)

            grad_cost,self.dcc_slopes=Grad_Penalty(real_inputs,fake_inputs,DCC,self.config)

            self.dcc_diff = self.dcc_fake_logit - self.dcc_real_logit
            self.dcc_gan_loss=tf.reduce_mean(self.dcc_diff)
            self.dcc_grad_loss=grad_cost
            self.dcc_loss=self.dcc_gan_loss+self.dcc_grad_loss#
            self.c_loss=-tf.reduce_mean(self.dcc_fake_logit)#

            self.summary_scalar('dcc_gan_loss',self.dcc_gan_loss)
            self.summary_scalar('dcc_grad_loss',self.dcc_grad_loss)
            self.summary_stats('dcc_slopes',self.dcc_slopes,hist=True)

            if config.optimizer == 'adam':
                optimizer = tf.train.AdamOptimizer
            else:
                raise Exception("[!] Caution! Optimizer untested {}. Only tested Adam".format(config.optimizer))
            self.c_optimizer, self.dcc_optimizer = optimizer(config.pt_cc_lr),optimizer(config.pt_dcc_lr)

            self.c_optim=self.c_optimizer.minimize(self.c_loss,var_list=self.train_var)
            self.dcc_optim=self.dcc_optimizer.minimize(self.dcc_loss,var_list=self.dcc_var)

            self.summary_stats('c_loss',self.c_loss)
            self.summary_stats('dcc_loss',self.c_loss)
            self.summary_stats('dcc_real_logit',self.dcc_real_logit,hist=True)
            self.summary_stats('dcc_fake_logit',self.dcc_fake_logit,hist=True)
