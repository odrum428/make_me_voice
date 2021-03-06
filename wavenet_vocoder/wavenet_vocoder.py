# -*- coding: utf-8 -*-
"""Wave-Net vocoder.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1slYjxT6q6ZtB9BiXB1aO5ueM6sfW3NiL
"""

!curl https://colab.chainer.org/install | sh -

!apt -y -q install aria2
!pip install -q librosa tqdm

!apt-get install -y -qq software-properties-common python-software-properties module-init-tools > /dev/null
!add-apt-repository -y ppa:alessandro-strada/ppa 2>&1 > /dev/null
!apt-get update -qq 2>&1 > /dev/null
!apt-get -y install -qq google-drive-ocamlfuse fuse > /dev/null

from google.colab import auth
auth.authenticate_user()

# Generate creds for the Drive FUSE library.
from oauth2client.client import GoogleCredentials
creds = GoogleCredentials.get_application_default()
import getpass
!google-drive-ocamlfuse -headless -id={creds.client_id} -secret={creds.client_secret} < /dev/null 2>&1 | grep URL
vcode = getpass.getpass()
!echo {vcode} | google-drive-ocamlfuse -headless -id={creds.client_id} -secret={creds.client_secret}

!mkdir -p drive
!google-drive-ocamlfuse drive
!mkdir -p drive/wavenet

import numpy as np

import chainer
import chainer.functions as F
import chainer.links as L
from chainer.training import extensions

chainer.print_runtime_info()

!aria2c -x5 http://homepages.inf.ed.ac.uk/jyamagis/release/VCTK-Corpus.tar.gz

!tar -xf VCTK-Corpus.tar.gz

!ls ./VCTK-Corpus/wav48

import IPython.display
IPython.display.Audio("./VCTK-Corpus/wav48/p226/p226_002.wav")

import librosa.display
import matplotlib.pyplot as plt
# %matplotlib inline


fig = plt.figure(figsize=(60,10))
a = librosa.load("./VCTK-Corpus/wav48/p225/p225_001.wav")
librosa.display.waveplot(a[0], sr=16000)

# training parameters
batchsize = 5  # Numer of audio clips in each mini-batch
length = 7680  # Number of samples in each audio clip
quantized_size = 256  # Number of quantizing audio data
epoch = 8  # Number of sweeps over the dataset to train
gpu_id = 0
seed = 0  # Random seed to split dataset into train and test

# display parameters
snapshot_interval = 1000  # Interval of snapshot
display_interval = 1000  # Interval of displaying log to console

# performance settings
process = 2  # Number of parallel processes
prefetch = 5  # Number of prefetch samples

# file settings
dataset_dir = './VCTK-Corpus'  # Directory of dataset
out_dir = './drive/wavenet/result'  # Directory to output the result

if gpu_id >= 0:
    chainer.global_config.autotune = True

class MuLaw(object):
    def __init__(self, mu=quantized_size, int_type=np.int32, float_type=np.float32):
        self.mu = mu
        self.int_type = int_type
        self.float_type = float_type

    def transform(self, x):
        x = x.astype(self.float_type)
        y = np.sign(x) * np.log(1 + self.mu * np.abs(x)) / np.log(1 + self.mu)
        y = np.digitize(y, 2 * np.arange(self.mu) / self.mu - 1) - 1
        return y.astype(self.int_type)

    def itransform(self, y):
        y = y.astype(self.float_type)
        y = 2 * y / self.mu - 1
        x = np.sign(y) / self.mu * ((1 + self.mu) ** np.abs(y) - 1)
        return x.astype(self.float_type)

import random
import librosa


class Preprocess(object):
    def __init__(self, sr, n_fft, hop_length, n_mels, top_db,
                 length, quantize):
        self.sr = sr
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.n_mels = n_mels
        self.top_db = top_db
        self.mu_law = MuLaw(quantize)
        self.quantize = quantize
        if length is None:
            self.length = None
        else:
            self.length = length + 1

    def __call__(self, path):
        # load data with trimming and normalizing
        raw, _ = librosa.load(path, self.sr, res_type='kaiser_fast')
        raw, _ = librosa.effects.trim(raw, self.top_db)
        raw /= np.abs(raw).max()
        raw = raw.astype(np.float32)

        # mu-law transform
        quantized = self.mu_law.transform(raw)

        # padding/triming
        if self.length is not None:
            if len(raw) <= self.length:
                # padding
                pad = self.length - len(raw)
                raw = np.concatenate(
                    (raw, np.zeros(pad, dtype=np.float32)))
                quantized = np.concatenate(
                    (quantized, self.quantize // 2 * np.ones(pad)))
                quantized = quantized.astype(np.int32)
            else:
                # triming
                start = random.randint(0, len(raw) - self.length - 1)
                raw = raw[start:start + self.length]
                quantized = quantized[start:start + self.length]

        # calculate mel-spectrogram
        spectrogram = librosa.feature.melspectrogram(
            raw, self.sr, n_fft=self.n_fft, hop_length=self.hop_length,
            n_mels=self.n_mels)
        spectrogram = librosa.power_to_db(spectrogram, ref=np.max)

        # normalize mel spectrogram into [-1, 1]
        spectrogram += 40
        spectrogram /= 40
        if self.length is not None:
            spectrogram = spectrogram[:, :self.length // self.hop_length]
        spectrogram = spectrogram.astype(np.float32)

        # expand dimensions
        one_hot = np.identity(
            self.quantize, dtype=np.float32)[quantized]
        one_hot = np.expand_dims(one_hot.T, 2)
        spectrogram = np.expand_dims(spectrogram, 2)
        quantized = np.expand_dims(quantized, 1)

        return one_hot[:, :-1], spectrogram, quantized[1:]

import pathlib


paths = sorted([str(path) for path in pathlib.Path(dataset_dir).glob('wav48/*/*.wav')])
preprocess = Preprocess(
    sr=16000, n_fft=1024, hop_length=256, n_mels=128, top_db=20,
    length=length, quantize=quantized_size)
dataset = chainer.datasets.TransformDataset(paths, preprocess)
train, valid = chainer.datasets.split_dataset_random(
    dataset, int(len(dataset) * 0.9), seed)

one_hot, spectrogram, quantized = dataset[0]

# 
print(one_hot.shape)
print(one_hot[:,0,0])

print(spectrogram.shape)
print(spectrogram)

print(quantized.shape)
print(quantized)

# Iterators
train_iter = chainer.iterators.MultiprocessIterator(
    train, batchsize, n_processes=process, n_prefetch=prefetch)
valid_iter = chainer.iterators.MultiprocessIterator(
    valid, batchsize, repeat=False, shuffle=False,
    n_processes=process, n_prefetch=prefetch)

# Model parameters for ResidualBlock
r_channels = 64  # Number of channels in residual layers and embedding
s_channels = 256  # Number of channels in the skip layers

class ResidualBlock(chainer.Chain):
    def __init__(self, filter_size, dilation,
                 residual_channels, dilated_channels, skip_channels):
        super(ResidualBlock, self).__init__()
        with self.init_scope():
            self.conv = L.DilatedConvolution2D(
                residual_channels, dilated_channels,
                ksize=(filter_size, 1),
                pad=(dilation * (filter_size - 1), 0), dilate=(dilation, 1))
            self.res = L.Convolution2D(
                dilated_channels // 2, residual_channels, 1)
            self.skip = L.Convolution2D(
                dilated_channels // 2, skip_channels, 1)

        self.filter_size = filter_size
        self.dilation = dilation
        self.residual_channels = residual_channels

    def __call__(self, x, condition):
        length = x.shape[2]
        h = self.conv(x)
        h = h[:, :, :length]  # crop
        h += condition
        tanh_z, sig_z = F.split_axis(h, 2, axis=1)
        z = F.tanh(tanh_z) * F.sigmoid(sig_z)
        if x.shape[2] == z.shape[2]:
            residual = self.res(z) + x
        else:
            residual = self.res(z) + x[:, :, -1:]  # crop
        skip_conenection = self.skip(z)
        return residual, skip_conenection

    def initialize(self, n):
        self.queue = chainer.Variable(self.xp.zeros((
            n, self.residual_channels,
            self.dilation * (self.filter_size - 1) + 1, 1),
            dtype=self.xp.float32))
        self.conv.pad = (0, 0)

    def pop(self, condition):
        return self(self.queue, condition)

    def push(self, x):
        self.queue = F.concat((self.queue[:, :, 1:], x), axis=2)

# Model parameters for ResidualNet
n_layer = 10  # Number of layers in each residual block
n_loop = 2  # Number of residual blocks

class ResidualNet(chainer.ChainList):
    def __init__(self, n_loop, n_layer, filter_size,
                 residual_channels, dilated_channels, skip_channels):
        super(ResidualNet, self).__init__()
        dilations = [2 ** i for i in range(n_layer)] * n_loop
        for dilation in dilations:
            self.add_link(ResidualBlock(
                filter_size, dilation,
                residual_channels, dilated_channels, skip_channels))

    def __call__(self, x, conditions):
        for i, (func, cond) in enumerate(zip(self.children(), conditions)):
            x, skip = func(x, cond)
            if i == 0:
                skip_connections = skip
            else:
                skip_connections += skip
        return skip_connections

    def initialize(self, n):
        for block in self.children():
            block.initialize(n)

    def generate(self, x, conditions):
        for i, (func, cond) in enumerate(zip(self.children(), conditions)):
            func.push(x)
            x, skip = func.pop(cond)
            if i == 0:
                skip_connections = skip
            else:
                skip_connections += skip
        return skip_connections

# Model parameters for WaveNet
a_channels = quantized_size  # Number of channels in the output layers
use_embed_tanh = True  # Use tanh after an initial 2x1 convolution

class WaveNet(chainer.Chain):
    def __init__(self, n_loop, n_layer, a_channels, r_channels, s_channels,
                 use_embed_tanh):
        super(WaveNet, self).__init__()
        with self.init_scope():
            self.embed = L.Convolution2D(
                a_channels, r_channels, (2, 1), pad=(1, 0), nobias=True)
            self.resnet = ResidualNet(
                n_loop, n_layer, 2, r_channels, 2 * r_channels, s_channels)
            self.proj1 = L.Convolution2D(
                s_channels, s_channels, 1, nobias=True)
            self.proj2 = L.Convolution2D(
                s_channels, a_channels, 1, nobias=True)
        self.a_channels = a_channels
        self.s_channels = s_channels
        self.use_embed_tanh = use_embed_tanh

    def __call__(self, x, condition, generating=False):
        length = x.shape[2]
        x = self.embed(x)
        x = x[:, :, :length, :]  # crop
        if self.use_embed_tanh:
            x = F.tanh(x)
        z = F.relu(self.resnet(x, condition))
        z = F.relu(self.proj1(z))
        y = self.proj2(z)
        return y

    def initialize(self, n):
        self.resnet.initialize(n)

        self.embed.pad = (0, 0)
        self.embed_queue = chainer.Variable(
            self.xp.zeros((n, self.a_channels, 2, 1), dtype=self.xp.float32))

        self.proj1_queue = chainer.Variable(self.xp.zeros(
            (n, self.s_channels, 1, 1), dtype=self.xp.float32))

        self.proj2_queue3 = chainer.Variable(self.xp.zeros(
            (n, self.s_channels, 1, 1), dtype=self.xp.float32))

    def generate(self, x, condition):
        self.embed_queue = F.concat((self.embed_queue[:, :, 1:], x), axis=2)
        x = self.embed(self.embed_queue)
        if self.use_embed_tanh:
            x = F.tanh(x)
        x = F.relu(self.resnet.generate(x, condition))

        self.proj1_queue = F.concat((self.proj1_queue[:, :, 1:], x), axis=2)
        x = F.relu(self.proj1(self.proj1_queue))

        self.proj2_queue3 = F.concat((self.proj2_queue3[:, :, 1:], x), axis=2)
        x = self.proj2(self.proj2_queue3)
        return x

class UpsampleNet(chainer.ChainList):
    def __init__(self, out_layers, r_channels,
                 channels=[128, 128], upscale_factors=[16, 16]):
        super(UpsampleNet, self).__init__()
        for channel, factor in zip(channels, upscale_factors):
            self.add_link(L.Deconvolution2D(
                None, channel, (factor, 1), stride=(factor, 1), pad=0))
        for i in range(out_layers):
            self.add_link(L.Convolution2D(None, 2 * r_channels, 1))
        self.n_deconvolutions = len(channels)

    def __call__(self, condition):
        conditions = []
        for i, link in enumerate(self.children()):
            if i < self.n_deconvolutions:
                condition = F.relu(link(condition))
            else:
                conditions.append(link(condition))
        return F.stack(conditions)

class EncoderDecoderModel(chainer.Chain):
    def __init__(self, encoder, decoder):
        super(EncoderDecoderModel, self).__init__()
        with self.init_scope():
            self.encoder = encoder
            self.decoder = decoder

    def __call__(self, x, condition):
        encoded_condition = self.encoder(condition)
        y = self.decoder(x, encoded_condition)
        return y

# Networks
encoder = UpsampleNet(n_loop * n_layer, r_channels)
decoder = WaveNet(
    n_loop, n_layer, a_channels, r_channels, s_channels, use_embed_tanh)
model = chainer.links.Classifier(EncoderDecoderModel(encoder, decoder))

# Optimizer
optimizer = chainer.optimizers.Adam(1e-4).setup(model)

# Updater and Trainer
updater = chainer.training.StandardUpdater(train_iter, optimizer, device=gpu_id)
trainer = chainer.training.Trainer(updater, (epoch, 'epoch'), out=out_dir)

# Extensions
snapshot_interval = (snapshot_interval, 'iteration')
display_interval = (display_interval, 'iteration')

trainer.extend(extensions.Evaluator(valid_iter, model, device=gpu_id))
trainer.extend(extensions.dump_graph('main/loss'))
trainer.extend(extensions.snapshot(), trigger=snapshot_interval)
trainer.extend(extensions.LogReport(trigger=display_interval))
trainer.extend(extensions.PrintReport(
    ['epoch', 'iteration', 'main/loss', 'main/accuracy',
     'validation/main/loss', 'validation/main/accuracy']),
    trigger=display_interval)
trainer.extend(extensions.PlotReport(
    ['main/loss', 'validation/main/loss'],
    'iteration', file_name='loss.png', trigger=display_interval))
trainer.extend(extensions.PlotReport(
    ['main/accuracy', 'validation/main/accuracy'],
    'iteration', file_name='accuracy.png', trigger=display_interval))
trainer.extend(extensions.ProgressBar(update_interval=100))

import glob
from natsort import natsorted


# Resume latest snapshot if exists
model_files = model_files = natsorted(glob.glob(out_dir + '/snapshot_iter_*'))
if len(model_files) > 0:
    resume = model_files[-1]
    print('model: {}'.format(resume))
    chainer.serializers.load_npz(resume, trainer)

# Run
trainer.run()

if gpu_id != -1:
    chainer.global_config.autotune = True
    use_gpu = True
    chainer.cuda.get_device_from_id(gpu_id).use()
else:
    use_gpu = False

import glob
import re
from natsort import natsorted

model_files = natsorted(glob.glob(out_dir + '/snapshot_iter_*'))
model_files

model_file = model_files[-1]
input_file = './VCTK-Corpus/wav48/p225/p225_002.wav'
output_file = './drive/wavenet/result.wav'

# Define networks
encoder = UpsampleNet(n_loop * n_layer, r_channels)
decoder = WaveNet(
    n_loop, n_layer,a_channels, r_channels, s_channels, use_embed_tanh)

# Load trained parameters
chainer.serializers.load_npz(
    model_file, encoder, 'updater/model:main/predictor/encoder/')
chainer.serializers.load_npz(
    model_file, decoder, 'updater/model:main/predictor/decoder/')

# Preprocess
_, condition, _ = Preprocess(
    sr=16000, n_fft=1024, hop_length=256, n_mels=128, top_db=20,
    length=None, quantize=a_channels)(input_file)
x = np.zeros([1, a_channels, 1, 1], dtype=np.float32)
condition = np.expand_dims(condition, axis=0)

import tqdm


# Non-autoregressive generate
if use_gpu:
    x = chainer.cuda.to_gpu(x, device=gpu_id)
    condition = chainer.cuda.to_gpu(condition, device=gpu_id)
    encoder.to_gpu(device=gpu_id)
    decoder.to_gpu(device=gpu_id)
x = chainer.Variable(x)
condition = chainer.Variable(condition)
conditions = encoder(condition)
decoder.initialize(1)
output = decoder.xp.zeros(conditions.shape[3])

# Autoregressive generate
for i in tqdm.tqdm(range(len(output))):
    with chainer.using_config('enable_backprop', False):
        out = decoder.generate(x, conditions[:, :, :, i:i + 1]).array
    value = decoder.xp.random.choice(
        a_channels, size=1,
        p=chainer.functions.softmax(out).array[0, :, 0, 0])[0]
    zeros = decoder.xp.zeros_like(x.array)
    zeros[:, value, :, :] = 1
    x = chainer.Variable(zeros)
    output[i] = value

# Save
if use_gpu:
    output = chainer.cuda.to_cpu(output)
wave = MuLaw(a_channels).itransform(output)
librosa.output.write_wav(output_file, wave, 16000)

import IPython.display
IPython.display.Audio(output_file)