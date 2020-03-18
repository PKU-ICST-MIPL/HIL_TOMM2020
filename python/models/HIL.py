import tensorflow as tf
from util import blocks
from my.tensorflow.nn import softsel, get_logits, highway_network, multi_conv1d, linear, conv2d, cosine_similarity, variable_summaries, dense_logits, fuse_gate
from my.tensorflow import flatten, reconstruct, add_wd, exp_mask
import numpy as np

class MyModel(object):
    def __init__(self, config, seq_length, emb_dim, hidden_dim, emb_train,  embeddings = None, pred_size = 3, context_seq_len = None, query_seq_len = None):
        ## Define hyperparameters
        # tf.reset_default_graph()
        self.embedding_dim = emb_dim
        self.dim = hidden_dim
        self.sequence_length = seq_length
        self.pred_size = pred_size 
        self.context_seq_len = context_seq_len
        self.query_seq_len = query_seq_len
        # self.config = config

        ## Define the placeholders    
        self.premise_x = tf.placeholder(tf.int32, [None, self.sequence_length], name='premise')
        self.hypothesis_x = tf.placeholder(tf.int32, [None, self.sequence_length], name='hypothesis')
        self.premise_pos = tf.placeholder(tf.int32, [None, self.sequence_length, 47], name='premise_pos')
        self.hypothesis_pos = tf.placeholder(tf.int32, [None, self.sequence_length, 47], name='hypothesis_pos')
        self.premise_char = tf.placeholder(tf.int32, [None, self.sequence_length, config.char_in_word_size], name='premise_char')
        self.hypothesis_char = tf.placeholder(tf.int32, [None, self.sequence_length, config.char_in_word_size], name='hypothesis_char')
        self.premise_exact_match = tf.placeholder(tf.int32, [None, self.sequence_length,1], name='premise_exact_match')
        self.hypothesis_exact_match = tf.placeholder(tf.int32, [None, self.sequence_length,1], name='hypothesis_exact_match')
        self.image_feature = tf.placeholder(tf.float32, [None,25088], name='image_feature')
        self.batch_len = tf.placeholder(tf.int32, [], name='batch_len')
        img_feature = tf.reshape(self.image_feature, [-1])
        img_feature = tf.reshape(img_feature, [-1,512,49])
        img_feature = tf.transpose(img_feature, [0,2,1])
        img_feature  = img_feature[:,0:32,:]
        self.global_step = tf.Variable(0, name='global_step', trainable=False)
        
        
        self.dropout_keep_rate = tf.train.exponential_decay(config.keep_rate, self.global_step, config.dropout_decay_step, config.dropout_decay_rate, staircase=False, name='dropout_keep_rate')
        config.keep_rate = self.dropout_keep_rate
        tf.summary.scalar('dropout_keep_rate', self.dropout_keep_rate)

        self.y = tf.placeholder(tf.int32, [None], name='label_y')
        self.keep_rate_ph = tf.placeholder(tf.float32, [], name='keep_prob')
        self.is_train = tf.placeholder('bool', [], name='is_train')
        
        ## Fucntion for embedding lookup and dropout at embedding layer
        def emb_drop(E, x):
            emb = tf.nn.embedding_lookup(E, x)
            emb_drop = tf.cond(self.is_train, lambda: tf.nn.dropout(emb, config.keep_rate), lambda: emb)
            return emb_drop

        # Get lengths of unpadded sentences    
        prem_seq_lengths, prem_mask = blocks.length(self.premise_x)  # mask [N, L , 1]
        hyp_seq_lengths, hyp_mask = blocks.length(self.hypothesis_x)
        self.prem_mask = prem_mask
        self.hyp_mask = hyp_mask


        #image mask的获得
        img_seq_lengths = 32
        populated = tf.ones([self.batch_len,32])
        img_mask = tf.cast(tf.expand_dims(populated, -1), tf.float32)
        self.img_mask = img_mask


        ### Embedding layer ###
        with tf.variable_scope("emb"):
            with tf.variable_scope("emb_var"), tf.device("/cpu:0"):
                self.E = tf.Variable(embeddings, trainable=emb_train)
                premise_in = emb_drop(self.E, self.premise_x)   #P
                hypothesis_in = emb_drop(self.E, self.hypothesis_x)  #H
    
        with tf.variable_scope("char_emb"):
            char_emb_mat = tf.get_variable("char_emb_mat", shape=[config.char_vocab_size, config.char_emb_size])
            with tf.variable_scope("char") as scope:
                char_pre = tf.nn.embedding_lookup(char_emb_mat, self.premise_char)
                char_hyp = tf.nn.embedding_lookup(char_emb_mat, self.hypothesis_char)

                filter_sizes = list(map(int, config.out_channel_dims.split(','))) #[100]
                heights = list(map(int, config.filter_heights.split(',')))        #[5]
                assert sum(filter_sizes) == config.char_out_size, (filter_sizes, config.char_out_size)
                with tf.variable_scope("conv") as scope:
                    conv_pre = multi_conv1d(char_pre, filter_sizes, heights, "VALID", self.is_train, config.keep_rate, scope='conv')
                    scope.reuse_variables()  
                    conv_hyp = multi_conv1d(char_hyp, filter_sizes, heights, "VALID", self.is_train, config.keep_rate, scope='conv')
                    conv_pre = tf.reshape(conv_pre, [-1, self.sequence_length, config.char_out_size])
                    conv_hyp = tf.reshape(conv_hyp, [-1, self.sequence_length, config.char_out_size])
            premise_in = tf.concat([premise_in, conv_pre], axis=2)
            hypothesis_in = tf.concat([hypothesis_in, conv_hyp], axis=2)

        
        premise_in = tf.concat((premise_in, tf.cast(self.premise_pos, tf.float32)), axis=2)
        hypothesis_in = tf.concat((hypothesis_in, tf.cast(self.hypothesis_pos, tf.float32)), axis=2)

        premise_in = tf.concat([premise_in, tf.cast(self.premise_exact_match, tf.float32)], axis=2)
        hypothesis_in = tf.concat([hypothesis_in, tf.cast(self.hypothesis_exact_match, tf.float32)], axis=2)
        
        with tf.variable_scope("image_fc") as scope:
            img_feature = tf.reshape(img_feature,[-1,512])
            
            W_fc = tf.get_variable('W_fc',shape=[512,448],initializer=tf.truncated_normal_initializer(mean=0.0, stddev=0.1,seed=1))
            b_fc = tf.get_variable('b_fc', [448], initializer=tf.constant_initializer(0.0))
            

            img_feature = tf.nn.tanh(tf.matmul(img_feature, W_fc) + b_fc)
            img_feature = tf.reshape(img_feature,[-1,32,448])    
        

        with tf.variable_scope("highway") as scope:
            premise_in = highway_network(premise_in, config.highway_num_layers, True, wd=config.wd, is_train=self.is_train,output_size = 350)    
            scope.reuse_variables()
            hypothesis_in = highway_network(hypothesis_in, config.highway_num_layers, True, wd=config.wd, is_train=self.is_train,output_size =350)


        with tf.variable_scope("img_highway") as scope:
            img_feature = highway_network(img_feature, config.highway_num_layers, True, wd=config.wd, is_train=self.is_train,output_size = 350)

        with tf.variable_scope("prepro") as scope:
            pre = premise_in
            hyp = hypothesis_in
            img = img_feature
            for i in range(config.self_att_enc_layers):
                with tf.variable_scope(tf.get_variable_scope(), reuse=False):
                    
                    p = cross_attention_layer(config, self.is_train, pre, hyp, p_mask=prem_mask, h_mask=hyp_mask, scope="{}_layer_self_att_enc".format(i))
                    h = cross_attention_layer(config, self.is_train, hyp, pre, p_mask=hyp_mask, h_mask=prem_mask, scope="{}_layer_self_att_enc_h".format(i))
                    
                    im = cross_attention_layer(config, self.is_train, img, hyp, p_mask=img_mask, h_mask=hyp_mask, scope="{}_layer_self_att_enc_im".format(i)) 
                    h_2 = cross_attention_layer(config, self.is_train, hyp, img, p_mask=hyp_mask, h_mask=img_mask, scope="{}_layer_self_att_enc_h_2".format(i))

                    

                    variable_summaries(p, "p_self_enc_summary_layer_{}".format(i))
                    variable_summaries(h, "h_self_enc_summary_layer_{}".format(i))
                    variable_summaries(im, "im_self_enc_summary_layer_{}".format(i))
                    variable_summaries(h_2, "h_2_self_enc_summary_layer_{}".format(i))

                
        with tf.variable_scope("main") as scope:

            def model_one_side(config, main, support,image,image_support, main_length, support_length,image_length, main_mask, support_mask,image_mask, scope):
                bi_att_mx = bi_attention_mx(config, self.is_train, main, support, p_mask=main_mask, h_mask=support_mask) # [N, PL, HL]
                bi_att_mx_2 = bi_attention_mx(config, self.is_train, image, image_support, p_mask=image_mask, h_mask=support_mask)
                
                bi_att_mx = tf.concat([bi_att_mx, bi_att_mx_2], axis=3)
                

                bi_att_mx = tf.cond(self.is_train, lambda: tf.nn.dropout(bi_att_mx, config.keep_rate), lambda: bi_att_mx)
                out_final = dense_net(config, bi_att_mx, self.is_train)
                
                return out_final



            premise_final = model_one_side(config, p, h,im,h_2,prem_seq_lengths, hyp_seq_lengths,img_seq_lengths, prem_mask, hyp_mask,img_mask, scope="premise_as_main")
            f0 = premise_final

            
    

        self.logits = linear(f0, self.pred_size ,True, bias_start=0.0, scope="logit", squeeze=False, wd=config.wd, input_keep_prob=config.keep_rate,
                                is_train=self.is_train)

        tf.summary.histogram('logit_histogram', self.logits)

        # Define the cost function
        self.total_cost = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(labels=self.y, logits=self.logits))
        self.acc = tf.reduce_mean(tf.cast(tf.equal(tf.arg_max(self.logits, dimension=1),tf.cast(self.y,tf.int64)), tf.float32))
        tf.summary.scalar('acc', self.acc)
        tf.summary.scalar('loss', self.total_cost)
        
        # L2 Loss
        if config.l2_loss:
            if config.sigmoid_growing_l2loss:
                weights_added = tf.add_n([tf.nn.l2_loss(tensor) for tensor in tf.trainable_variables() if tensor.name.endswith("weights:0") and not tensor.name.endswith("weighted_sum/weights:0") or tensor.name.endswith('kernel:0')])
                full_l2_step = tf.constant(config.weight_l2loss_step_full_reg , dtype=tf.int32, shape=[], name='full_l2reg_step')
                full_l2_ratio = tf.constant(config.l2_regularization_ratio , dtype=tf.float32, shape=[], name='l2_regularization_ratio')
                gs_flt = tf.cast(self.global_step , tf.float32)
                half_l2_step_flt = tf.cast(full_l2_step / 2 ,tf.float32)

                l2loss_ratio = tf.sigmoid( ((gs_flt - half_l2_step_flt) * 8) / half_l2_step_flt) * full_l2_ratio
                tf.summary.scalar('l2loss_ratio', l2loss_ratio)
                l2loss = weights_added * l2loss_ratio
            else:
                l2loss = tf.add_n([tf.nn.l2_loss(tensor) for tensor in tf.trainable_variables() if tensor.name.endswith("weights:0") or tensor.name.endswith('kernel:0')]) * tf.constant(config.l2_regularization_ratio , dtype='float', shape=[], name='l2_regularization_ratio')
            tf.summary.scalar('l2loss', l2loss)
            self.total_cost += l2loss

        if config.wo_enc_sharing or config.wo_highway_sharing_but_penalize_diff:
            diffs = []
            for i in range(config.self_att_enc_layers):
                for tensor in tf.trainable_variables():
                    print(tensor.name)
                    if tensor.name == "prepro/{}_layer_self_att_enc/bi_attention/h_logits/first/kernel:0".format(i):
                        l_lg = tensor 
                    elif tensor.name == "prepro/{}_layer_self_att_enc_h/bi_attention/h_logits/first/kernel:0".format(i):
                        r_lg = tensor
                    if tensor.name == "prepro/{}_layer_self_att_enc_im/bi_attention/h_logits/first/kernel:0".format(i):
                        l_lg_im = tensor
                    elif tensor.name == "prepro/{}_layer_self_att_enc_h_2/bi_attention/h_logits/first/kernel:0".format(i):
                        r_lg_2 = tensor
                    
                    elif tensor.name == "prepro/{}_layer_self_att_enc/self_att_fuse_gate/lhs_1/kernel:0".format(i):    
                        l_fg_lhs_1 = tensor 
                    elif tensor.name == "prepro/{}_layer_self_att_enc_h/self_att_fuse_gate/lhs_1/kernel:0".format(i):
                        r_fg_lhs_1= tensor
                    elif tensor.name == "prepro/{}_layer_self_att_enc_im/self_att_fuse_gate/lhs_1/kernel:0".format(i):
                        l_fg_lhs_1_im = tensor
                    elif tensor.name == "prepro/{}_layer_self_att_enc_h_2/self_att_fuse_gate/lhs_1/kernel:0".format(i):
                        r_fg_lhs_1_2= tensor

                    elif tensor.name == "prepro/{}_layer_self_att_enc/self_att_fuse_gate/rhs_1/kernel:0".format(i):
                        l_fg_rhs_1= tensor
                    elif tensor.name == "prepro/{}_layer_self_att_enc_h/self_att_fuse_gate/rhs_1/kernel:0".format(i):
                        r_fg_rhs_1= tensor

                    elif tensor.name == "prepro/{}_layer_self_att_enc_im/self_att_fuse_gate/rhs_1/kernel:0".format(i):
                        l_fg_rhs_1_im= tensor
                    elif tensor.name == "prepro/{}_layer_self_att_enc_h_2/self_att_fuse_gate/rhs_1/kernel:0".format(i):
                        r_fg_rhs_1_2= tensor


                    elif tensor.name == "prepro/{}_layer_self_att_enc/self_att_fuse_gate/lhs_2/kernel:0".format(i):
                        l_fg_lhs_2= tensor
                    elif tensor.name == "prepro/{}_layer_self_att_enc_h/self_att_fuse_gate/lhs_2/kernel:0".format(i):
                        r_fg_lhs_2= tensor

                    elif tensor.name == "prepro/{}_layer_self_att_enc_im/self_att_fuse_gate/lhs_2/kernel:0".format(i):
                        l_fg_lhs_2_im= tensor
                    elif tensor.name == "prepro/{}_layer_self_att_enc_h_2/self_att_fuse_gate/lhs_2/kernel:0".format(i):
                        r_fg_lhs_2_2= tensor

                   
                    elif tensor.name == "prepro/{}_layer_self_att_enc/self_att_fuse_gate/rhs_2/kernel:0".format(i):
                        l_fg_rhs_2= tensor
                    elif tensor.name == "prepro/{}_layer_self_att_enc_h/self_att_fuse_gate/rhs_2/kernel:0".format(i):
                        r_fg_rhs_2= tensor

                    elif tensor.name == "prepro/{}_layer_self_att_enc_im/self_att_fuse_gate/rhs_2/kernel:0".format(i):
                        l_fg_rhs_2_im= tensor
                    elif tensor.name == "prepro/{}_layer_self_att_enc_h_2/self_att_fuse_gate/rhs_2/kernel:0".format(i):
                        r_fg_rhs_2_2= tensor
                    
                    if config.two_gate_fuse_gate:
                        if tensor.name == "prepro/{}_layer_self_att_enc/self_att_fuse_gate/lhs_3/kernel:0".format(i):    
                            l_fg_lhs_3 = tensor 
                        elif tensor.name == "prepro/{}_layer_self_att_enc_h/self_att_fuse_gate/lhs_3/kernel:0".format(i):
                            r_fg_lhs_3 = tensor
                        if tensor.name == "prepro/{}_layer_self_att_enc_im/self_att_fuse_gate/lhs_3/kernel:0".format(i):
                            l_fg_lhs_3_im = tensor
                        elif tensor.name == "prepro/{}_layer_self_att_enc_h_2/self_att_fuse_gate/lhs_3/kernel:0".format(i):
                            r_fg_lhs_3_2 = tensor
                        elif tensor.name == "prepro/{}_layer_self_att_enc/self_att_fuse_gate/rhs_3/kernel:0".format(i):
                            l_fg_rhs_3 = tensor
                        elif tensor.name == "prepro/{}_layer_self_att_enc_h/self_att_fuse_gate/rhs_3/kernel:0".format(i):
                            r_fg_rhs_3 = tensor

                        elif tensor.name == "prepro/{}_layer_self_att_enc_im/self_att_fuse_gate/rhs_3/kernel:0".format(i):
                            l_fg_rhs_3_im = tensor
                        elif tensor.name == "prepro/{}_layer_self_att_enc_h_2/self_att_fuse_gate/rhs_3/kernel:0".format(i):
                            r_fg_rhs_3_2 = tensor


                diffs += [l_lg - r_lg, l_fg_lhs_1 - r_fg_lhs_1, l_fg_rhs_1 - r_fg_rhs_1, l_fg_lhs_2 - r_fg_lhs_2, l_fg_rhs_2 - r_fg_rhs_2, l_lg_im - r_lg_2, l_fg_lhs_1_im - r_fg_lhs_1_2, l_fg_rhs_1_im - r_fg_rhs_1_2, l_fg_lhs_2_im - r_fg_lhs_2_2, l_fg_rhs_2_im - r_fg_rhs_2_2]
                if config.two_gate_fuse_gate:
                    diffs += [l_fg_lhs_3_im - r_fg_lhs_3_2, l_fg_rhs_3_im - r_fg_rhs_3_2]
            

            diff_loss = tf.add_n([tf.nn.l2_loss(tensor) for tensor in diffs]) * tf.constant(config.diff_penalty_loss_ratio , dtype='float', shape=[], name='diff_penalty_loss_ratio')
            tf.summary.scalar('diff_penalty_loss', diff_loss)
            self.total_cost += diff_loss


        self.summary = tf.summary.merge_all()

        total_parameters = 0
        for v in tf.global_variables():
            if not v.name.endswith("weights:0") and not v.name.endswith("biases:0") and not v.name.endswith('kernel:0') and not v.name.endswith('bias:0'):
                continue
            print(v.name)
            # print(type(v.name))
            shape = v.get_shape().as_list()
            param_num = 1
            for dim in shape:
                param_num *= dim 
            print(param_num)
            total_parameters += param_num
        print(total_parameters)



def weight_variable(shape):
  initial = tf.truncated_normal(shape, stddev=0.1)
  return tf.Variable(initial)

def bias_variable(shape):
  initial = tf.constant(0.1, shape=shape)
  return tf.Variable(initial)


def bi_attention_mx(config, is_train, p, h, p_mask=None, h_mask=None, scope=None): #[N, L, 2d]
    with tf.variable_scope(scope or "dense_logit_bi_attention"):
        PL = p.get_shape().as_list()[1]
        HL = h.get_shape().as_list()[1]
        p_aug = tf.tile(tf.expand_dims(p, 2), [1,1,HL,1])
        h_aug = tf.tile(tf.expand_dims(h, 1), [1,PL,1,1]) #[N, PL, HL, 2d]

        if p_mask is None:
            ph_mask = None
        else:
            p_mask_aug = tf.reduce_any(tf.cast(tf.tile(tf.expand_dims(p_mask, 2), [1, 1, HL, 1]), tf.bool), axis=3)
            h_mask_aug = tf.reduce_any(tf.cast(tf.tile(tf.expand_dims(h_mask, 1), [1, PL, 1, 1]), tf.bool), axis=3)
            ph_mask = p_mask_aug & h_mask_aug
        ph_mask = None

        
        h_logits = p_aug * h_aug
        
        return h_logits


def self_attention(config, is_train, p, p_mask=None, scope=None): #[N, L, 2d]
    with tf.variable_scope(scope or "self_attention"):
        PL = p.get_shape().as_list()[1]
        dim = p.get_shape().as_list()[-1]
        # HL = tf.shape(h)[1]
        p_aug_1 = tf.tile(tf.expand_dims(p, 2), [1,1,PL,1])
        p_aug_2 = tf.tile(tf.expand_dims(p, 1), [1,PL,1,1]) #[N, PL, HL, 2d]

        if p_mask is None:
            ph_mask = None
        else:
            p_mask_aug_1 = tf.reduce_any(tf.cast(tf.tile(tf.expand_dims(p_mask, 2), [1, 1, PL, 1]), tf.bool), axis=3)
            p_mask_aug_2 = tf.reduce_any(tf.cast(tf.tile(tf.expand_dims(p_mask, 1), [1, PL, 1, 1]), tf.bool), axis=3)
            self_mask = p_mask_aug_1 & p_mask_aug_2


        h_logits = get_logits([p_aug_1, p_aug_2], None, True, wd=config.wd, mask=self_mask,
                              is_train=is_train, func=config.self_att_logit_func, scope='h_logits')  # [N, PL, HL]
        self_att = softsel(p_aug_2, h_logits) 

        return self_att


def self_attention_layer(config, is_train, p, p_mask=None, scope=None):
    with tf.variable_scope(scope or "self_attention_layer"):
        PL = tf.shape(p)[1]
        # HL = tf.shape(h)[1]
        # if config.q2c_att or config.c2q_att:
        self_att = self_attention(config, is_train, p, p_mask=p_mask)

        print("self_att shape")
        print(self_att.get_shape())
        
        p0 = fuse_gate(config, is_train, p, self_att, scope="self_att_fuse_gate")
        
        return p0




def bi_attention(config, is_train, p, h, p_mask=None, h_mask=None, scope=None, h_value = None): #[N, L, 2d]
     with tf.variable_scope(scope or "bi_attention"):
         PL = tf.shape(p)[1]
         HL = tf.shape(h)[1]
         p_aug = tf.tile(tf.expand_dims(p, 2), [1,1,HL,1])
         h_aug = tf.tile(tf.expand_dims(h, 1), [1,PL,1,1]) #[N, PL, HL, 2d]


         if p_mask is None:
             ph_mask = None
         else:
             p_mask_aug = tf.reduce_any(tf.cast(tf.tile(tf.expand_dims(p_mask, 2), [1, 1, HL, 1]), tf.bool), axis=3)
             h_mask_aug = tf.reduce_any(tf.cast(tf.tile(tf.expand_dims(h_mask, 1), [1, PL, 1, 1]), tf.bool), axis=3)
             ph_mask = p_mask_aug & h_mask_aug


         h_logits = get_logits([p_aug, h_aug], None, True, wd=config.wd, mask=ph_mask,
                           is_train=is_train, func="mul_linear", scope='h_logits')  # [N, PL, HL]
         h_a = softsel(h_aug, h_logits) 
         p_a = softsel(p, tf.reduce_max(h_logits, 2))  # [N, 2d]
         p_a = tf.tile(tf.expand_dims(p_a, 1), [1, PL, 1]) # 

         return h_a, p_a

def cross_attention_layer(config, is_train, p, h, p_mask=None, h_mask=None, scope=None, tensor_dict=None):
    with tf.variable_scope(scope or "cross_attention_layer"):
        PL = tf.shape(p)[1]
        HL = tf.shape(h)[1]
        # if config.q2c_att or config.c2q_att:
        h_a, p_a = bi_attention(config, is_train, p, h, p_mask=p_mask, h_mask=h_mask)
       
        att_wo_pa = True 
        if att_wo_pa:
            p0 = tf.concat([p, h_a, p * h_a], axis=2)
        else:
            p0 = tf.concat([p, h_a, p * h_a, p * p_a], axis=2)
        # else:
        #     p0 = tf.concat(3, [p, u_a, p * u_a])
        p1 = linear(p0, p.get_shape().as_list()[-1] ,True, bias_start=0.0, scope="cross_att_linear_scale", squeeze=False, wd=config.wd, input_keep_prob=config.keep_rate, is_train=is_train)
        """
        if config.cross_att_residual_conn:
            return p + p1
        elif config.cross_att_fuse_gate_residual_conn:
            return fuse_gate(config, is_train, p, p1, scope="cross_att_fuse_gate")
        else:
            return p1
        """
        p2 = fuse_gate(config, is_train, p, p1, scope="self_att_fuse_gate")

        return p2



def dense_net(config, denseAttention, is_train):
    with tf.variable_scope("dense_net"):
        
        dim = denseAttention.get_shape().as_list()[-1]
        act = tf.nn.relu if config.first_scale_down_layer_relu else None
        fm = tf.contrib.layers.convolution2d(denseAttention, int(dim * config.dense_net_first_scale_down_ratio), config.first_scale_down_kernel, padding="SAME", activation_fn = act)

        fm = dense_net_block(config, fm, config.dense_net_growth_rate, config.dense_net_layers, config.dense_net_kernel_size, is_train ,scope = "first_dense_net_block") 
        fm = dense_net_transition_layer(config, fm, config.dense_net_transition_rate, scope='second_transition_layer')
        fm = dense_net_block(config, fm, config.dense_net_growth_rate, config.dense_net_layers, config.dense_net_kernel_size, is_train ,scope = "second_dense_net_block") 
        fm = dense_net_transition_layer(config, fm, config.dense_net_transition_rate, scope='third_transition_layer')
        fm = dense_net_block(config, fm, config.dense_net_growth_rate, config.dense_net_layers, config.dense_net_kernel_size, is_train ,scope = "third_dense_net_block") 

        fm = dense_net_transition_layer(config, fm, config.dense_net_transition_rate, scope='fourth_transition_layer')

        shape_list = fm.get_shape().as_list()
        print(shape_list)
        out_final = tf.reshape(fm, [-1, shape_list[1]*shape_list[2]*shape_list[3]])
        return out_final



def dense_net_block(config, feature_map, growth_rate, layers, kernel_size, is_train, padding="SAME", act=tf.nn.relu, scope=None):
    with tf.variable_scope(scope or "dense_net_block"):
        conv2d = tf.contrib.layers.convolution2d
        dim = feature_map.get_shape().as_list()[-1]

        list_of_features = [feature_map]
        features = feature_map
        for i in range(layers):        
            ft = conv2d(features, growth_rate, (kernel_size, kernel_size), padding=padding, activation_fn=act)
            list_of_features.append(ft)
            features = tf.concat(list_of_features, axis=3)

        print("dense net block out shape")
        print(features.get_shape().as_list())
        return features 

def dense_net_transition_layer(config, feature_map, transition_rate, scope=None):
    with tf.variable_scope(scope or "transition_layer"):


        out_dim = int(feature_map.get_shape().as_list()[-1] * transition_rate)
        feature_map = tf.contrib.layers.convolution2d(feature_map, out_dim, 1, padding="SAME", activation_fn = None)
        
        
        feature_map = tf.nn.max_pool(feature_map, [1,2,2,1],[1,2,2,1], "VALID")

        print("Transition Layer out shape")
        print(feature_map.get_shape().as_list())
        return feature_map


