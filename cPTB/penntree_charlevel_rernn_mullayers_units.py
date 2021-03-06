from __future__ import print_function
import sys
import argparse
import os

import time

import lasagne
import theano
import numpy as np
import theano.tensor as T

from lasagne.layers import InputLayer,ReshapeLayer,DimshuffleLayer,Gate, DenseLayer
from lasagne.layers import ConcatLayer,NonlinearityLayer,DropoutLayer, SliceLayer,ElemwiseSumLayer
from lasagne.nonlinearities import softmax, rectify,tanh,very_leaky_rectify
from lasagne.init import Uniform,Normal,HeNormal

from IndRNN_onlyrecurrent import IndRNNLayer_onlyrecurrent as indrnn_onlyrecurrent
from BatchNorm_step_timefirst import BatchNorm_step_timefirst_Layer


#np.set_printoptions(threshold=3000,edgeitems=50)
sys.setrecursionlimit(50000)
parser = argparse.ArgumentParser(description='IndRNN for the char level PennTreeBank Language Model')
parser.add_argument('--hidden_units', type=int, default=2000)
parser.add_argument('--batch_size', type=int, default=128,help='batch_size')
parser.add_argument('--seq_len', type=int, default=50,help='seq_len')
parser.add_argument('--num_layers', type=int, default=6,help='num_layers')
parser.add_argument('--lr', type=np.float32, default=2e-4, help='lr')
parser.add_argument('--act', type=str, default='relu', help='act')
parser.add_argument('--data_aug', action='store_true', default=False, help='to start from different positions at each training epoch')
parser.add_argument('--gradclipvalue', type=np.float32, default=10)
parser.add_argument('--MAG', type=int, default=2)
parser.add_argument('--fix_bound', action='store_true', default=False)

#bn
parser.add_argument('--use_bn_afterrnn', action='store_true', default=False)
parser.add_argument('--use_bn_embed', action='store_true', default=False)

#drop
parser.add_argument('--use_dropout', action='store_true', default=False)
parser.add_argument('--use_drophiddeninput', action='store_true', default=False)
parser.add_argument('--droprate', type=np.float32, default=0.3)
parser.add_argument('--droplayers', type=int, default=1)
parser.add_argument('--drop_batchax', action='store_true', default=False)

#residual
parser.add_argument('--use_residual', action='store_true', default=False)
parser.add_argument('--residual_layers', type=int, default=2)
parser.add_argument('--residual_block', type=int, default=3)
parser.add_argument('--unit_factor', type=np.float32, default=1)

#weight decay
parser.add_argument('--use_weightdecay_nohiddenW', action='store_true', default=False)
parser.add_argument('--decayfactor', type=np.float32, default=1e-4)

#initialization
parser.add_argument('--ini_in2hid', type=np.float32, default=0.005)
parser.add_argument('--ini_b', type=np.float32, default=0.0)

#others
parser.add_argument('--epsilon', type=np.float32, default=1e-4)
args = parser.parse_args()
print (args)


num_layers=args.num_layers
droplayers=args.droplayers
outputclass=50
batch_size = args.batch_size
seq_len=args.seq_len
hidden_units=args.hidden_units
use_bn_embed=args.use_bn_embed
use_dropout=args.use_dropout
lr=np.float32(args.lr)
droprate=np.float32(args.droprate)
opti=lasagne.updates.adam  

rnnmodel=indrnn_onlyrecurrent
act=rectify  
if args.act=='tanh':
  act=tanh  



from reader import data_iterator, ptb_raw_data
name_dataset='ptb.char.'
def get_raw_data(dataset='ptb',data_path='data/'):
  raw_data = ptb_raw_data(data_path,filename=name_dataset)
  return raw_data
train_data, valid_data, test_data, _ = get_raw_data('ptb')
epoch_size =((len(train_data) // batch_size) - 1) // seq_len


seq_len1=len(train_data)
U_bound=pow(args.MAG, 1.0 / seq_len1)
if args.act=='tanh':
  U_bound=pow(args.MAG/(pow(0.9,seq_len1/10.0)), 1.0 / seq_len1)
if args.fix_bound:
  U_bound=1.0
#Because the last state of one batch is used as the initial state of the next batch, the total length is used here.
# This bound can simply set to 1. (1) the sequence is too long and they are already very close to 1. 
#(2) Due to the precision of GPU, if it is rounded to a larger value, it may explode.

taxdrop= (0,) 
if args.drop_batchax:
  taxdrop= (0,1,) 

ini_W=HeNormal(gain=np.sqrt(2)/2.0)
if args.use_bn_afterrnn:
  ini_W=Normal(args.ini_in2hid)
  
units=[]
acc_units=[]
acc_units.append(0)
sum_units=0
if args.unit_factor!=1 and num_layers%(args.residual_block*args.residual_layers)!=args.start_residual:
  print ('layers should be layers = args.residual_block*args.residual_layers +1')
  assert 2==3
for l in range(num_layers):
  units_inc_factor=1
  if l>=1:
    units_inc_factor=np.power(args.unit_factor, (l-1)//(args.residual_block*args.residual_layers))
  units.append(np.int(hidden_units*units_inc_factor))
  sum_units+=np.int(hidden_units*units_inc_factor)
  acc_units.append(sum_units)
  
#print(units,acc_units)  
def build_rnn_network(rnnmodel,X_sym,hid_init_sym):
    net = {}    
    
    net['input0'] = InputLayer((batch_size, seq_len),X_sym)        
    net['input']=lasagne.layers.EmbeddingLayer(net['input0'],outputclass,units[0])#,W=lasagne.init.Uniform(inial_scale)      
    net['rnn0']=DimshuffleLayer(net['input'],(1,0,2)) #change to (time, batch_size,hidden_units)    
    if use_bn_embed:
      net['rnn0']=BatchNorm_step_timefirst_Layer(net['rnn0'],axes=(0,1),epsilon=args.epsilon )
      
    for l in range(1, num_layers+1):
      net['hiddeninput%d'%l] = InputLayer((batch_size, units[l-1]),hid_init_sym[:,acc_units[l-1]:acc_units[l]])               
      net['rnn%d'%(l-1)]=ReshapeLayer(net['rnn%d'%(l-1)], (batch_size* seq_len, -1))          
      net['rnn%d'%(l-1)]=DenseLayer(net['rnn%d'%(l-1)],units[l-1],W=ini_W,b=lasagne.init.Constant(args.ini_b),nonlinearity=None)  #W=Uniform(ini_rernn_in_to_hid),         #
      net['rnn%d'%(l-1)]=ReshapeLayer(net['rnn%d'%(l-1)], (seq_len, batch_size,  -1))  

      if args.use_residual and l>args.residual_layers and (l-1)%args.residual_layers==0:# and l!=num_layers
        if units[l - 1]!=units[l - 1 - args.residual_layers]:
          net['leftbranch%d' % (l - 1)] = ReshapeLayer(net['sum%d'%(l-args.residual_layers)], (batch_size * seq_len, -1))
          net['leftbranch%d' % (l - 1)] = DenseLayer(net['leftbranch%d' % (l - 1)], units[l - 1], W=ini_W, nonlinearity=None)
          net['leftbranch%d' % (l - 1)] = ReshapeLayer(net['leftbranch%d' % (l - 1)], (seq_len, batch_size, -1))
          net['leftbranch%d' % (l - 1)] = BatchNorm_step_timefirst_Layer(net['leftbranch%d' % (l - 1)], axes=(0, 1), epsilon=args.epsilon)
          print('left branch')
        else:
          net['leftbranch%d' % (l - 1)] = net['sum%d'%(l-args.residual_layers)]
        net['sum%d'%l]=ElemwiseSumLayer((net['rnn%d'%(l-1)],net['leftbranch%d' % (l - 1)]))
      else:
        net['sum%d'%l]=net['rnn%d'%(l-1)]      
      
      net['rnn%d'%l]=net['sum%d'%l]
      if not args.use_bn_afterrnn:
        net['rnn%d'%l]=BatchNorm_step_timefirst_Layer(net['rnn%d'%l],axes= (0,1),beta=lasagne.init.Constant(args.ini_b),epsilon=args.epsilon)    
               
      ini_hid_start=0
      if act==tanh:
        ini_hid_start=-1*U_bound
      net['rnn%d'%l]=rnnmodel(net['rnn%d'%l],units[l-1],hid_init=net['hiddeninput%d'%l],W_hid_to_hid=Uniform(range=(ini_hid_start,U_bound)),nonlinearity=act,only_return_final=False, grad_clipping=args.gradclipvalue)      
                
      net['last_state%d'%l]=SliceLayer(net['rnn%d'%l],-1, axis=0)
      if l==1:
        net['hid_out']=net['last_state%d'%l]
      else:
        net['hid_out']=ConcatLayer([net['hid_out'], net['last_state%d'%l]],axis=1)
                                             
      if use_dropout and l%droplayers==0 and not args.bn_drop:
        net['rnn%d'%l]=lasagne.layers.DropoutLayer(net['rnn%d'%l], p=droprate, shared_axes=taxdrop)                      

      if args.use_bn_afterrnn:
        net['rnn%d'%l]=BatchNorm_step_timefirst_Layer(net['rnn%d'%l],axes= (0,1),epsilon=args.epsilon)                                                 
        
    net['rnn%d'%num_layers]=DimshuffleLayer(net['rnn%d'%num_layers],(1,0,2))   
    net['reshape_rnn']=ReshapeLayer(net['rnn%d'%num_layers],(-1,units[num_layers-1]))        
    net['out']=DenseLayer(net['reshape_rnn'],outputclass,nonlinearity=softmax)#lasagne.init.HeNormal(gain='relu'))#,W=Uniform(inial_scale)
    return net
  

X_sym = T.imatrix('inputs')#,dtype=theano.config.floatX)
y_sym = T.imatrix('label')#,dtype=theano.config.floatX)    
hid_init_sym = T.matrix()#tensor3()

learn_net=build_rnn_network(rnnmodel,X_sym,hid_init_sym)
 
y_sym0=y_sym.reshape((-1,))
prediction,hid_rec_init = lasagne.layers.get_output([learn_net['out'],learn_net['hid_out']],deterministic=False) # {X_sym:X_sym,hid_init_sym:hid_init_sym},                        
loss = lasagne.objectives.categorical_crossentropy(prediction, y_sym0).mean()
perp=T.exp(loss)
bpc = (loss/np.log(2.0))

cost=loss
  
if args.use_weightdecay_nohiddenW:
  params = lasagne.layers.get_all_params(learn_net['out'], regularizable=True)
  for para in params:
    if para.name!='hidden_to_hidden.W':
      cost += args.decayfactor*lasagne.regularization.apply_penalty(para, lasagne.regularization.l2)#*T.clip(T.abs_(para)-1,0,100))     
  
params = lasagne.layers.get_all_params(learn_net['out'], trainable=True)

learning_ratetrain = T.scalar(name='learning_ratetrain',dtype=theano.config.floatX)

grads = theano.grad(cost, params)
# if use_gradclip:
#   grads= [T.clip(g, -gradclipvalue, gradclipvalue) for g in grads]
updates = opti( grads, params, learning_rate=learning_ratetrain)#rmsprop( grads, params, learning_rate=learning_ratetrain)#nesterov_momentum
print('Compiling')
train_fn = theano.function([X_sym, y_sym,hid_init_sym,learning_ratetrain],\
                            [perp, bpc, hid_rec_init], updates=updates)

test_prediction, test_hid_rec_init = lasagne.layers.get_output([learn_net['out'],learn_net['hid_out']], \
                                                               deterministic=True,batch_norm_use_averages=False)#{X_sym:X_sym,hid_init_sym:hid_init_sym},

test_loss = lasagne.objectives.categorical_crossentropy(test_prediction, y_sym0).mean()
test_perp=T.exp(test_loss)
test_bpc = (test_loss/np.log(2.0))
test_fn = theano.function([X_sym, y_sym,hid_init_sym],\
                            [test_perp, test_bpc, test_hid_rec_init])

bn_test_prediction, bn_test_hid_rec_init = lasagne.layers.get_output([learn_net['out'],learn_net['hid_out']], \
                                                                     deterministic=True)#{X_sym:X_sym,hid_init_sym:hid_init_sym},

bn_test_loss = lasagne.objectives.categorical_crossentropy(bn_test_prediction, y_sym0).mean()
bn_test_perp=T.exp(bn_test_loss)
bn_test_bpc = (bn_test_loss/np.log(2.0))
bn_test_fn = theano.function([X_sym, y_sym,hid_init_sym],\
                            [bn_test_perp, bn_test_bpc, bn_test_hid_rec_init])




learning_rate=np.float32(lr)

t_prep=0
t_bpc=0
count=0
lastbpc=100
patience=0
patienceThre=2

for epoci in range(1,10000):  
  hid_init=np.zeros((batch_size, sum_units), dtype='float32')
  if args.data_aug:
    dropindex=np.random.randint(seq_len*5)  
  for batchi, (x, y) in enumerate(data_iterator(train_data[dropindex:], batch_size, seq_len)):
    if rnnmodel==indrnn_onlyrecurrent:
      for para in params:
        if para.name=='hidden_to_hidden.W':
          para.set_value(np.clip(para.get_value(),-1*U_bound,U_bound)) 
    if args.use_drophiddeninput and np.random.randint(2)==1:
      temp=np.float32(np.random.randint(2,size=(sum_units,)))
      temp=temp[np.newaxis,:]
      hid_init=hid_init*temp
    perp, bpc, hid_init=train_fn(x, y,hid_init,learning_rate)

    if np.isnan(perp):
      print ('NaN detected in cost')
      assert(2==3)
    if np.isinf(perp):
      print ('INF detected in cost')
      assert(2==3)  
    t_prep+=perp
    t_bpc+=bpc
    count+=1 
    
  trainbpc=t_bpc/count
  print ('prep','bpc',t_prep/count, t_bpc/count)
  train_acc=t_prep/count
  count=0
  t_prep=0
  t_bpc=0 
  
  hid_init=np.zeros((batch_size, sum_units), dtype='float32')
  for testbatchi, (x, y) in enumerate(data_iterator(valid_data, batch_size, seq_len)):
    perp, bpc, hid_init=bn_test_fn(x, y,hid_init)
    t_prep+=perp
    t_bpc+=bpc
    count+=1
  print ('bn_validprep','bn_validbpc',t_prep/count, t_bpc/count )
  validbpc=t_bpc/count
  count=0
  t_prep=0
  t_bpc=0

  hid_init=np.zeros((batch_size, sum_units), dtype='float32')
  for testbatchi, (x, y) in enumerate(data_iterator(test_data, batch_size, seq_len)):
    perp, bpc, hid_init=test_fn(x, y,hid_init)
    t_prep+=perp
    t_bpc+=bpc
    count+=1
  print ('testprep','testbpc',t_prep/count, t_bpc/count )
  test_acc=t_prep/count  
  count=0
  t_prep=0
  t_bpc=0
  
  
  hid_init=np.zeros((batch_size, sum_units), dtype='float32')
  for testbatchi, (x, y) in enumerate(data_iterator(test_data, batch_size, seq_len)):
    perp, bpc, hid_init=bn_test_fn(x, y,hid_init)
    t_prep+=perp
    t_bpc+=bpc
    count+=1
  print ('bn_testprep','bn_testbpc',t_prep/count, t_bpc/count )
  #test_acc=t_prep/count  
  count=0
  t_prep=0
  t_bpc=0  
  
  if (validbpc <lastbpc):
    best_para=lasagne.layers.get_all_param_values(learn_net['out'])  
    lastbpc=  validbpc
    patience=0
  elif patience>patienceThre:
    learning_rate=np.float32(learning_rate*0.2)
    print ('learning rate',learning_rate)
    lasagne.layers.set_all_param_values(learn_net['out'], best_para)
    patience=0
    if learning_rate<1e-6:
      break
  else:
    patience+=1
    
save_name='indrnn_cPTB'+str(seq_len)
np.savez(save_name, *lasagne.layers.get_all_param_values(learn_net['out']))
