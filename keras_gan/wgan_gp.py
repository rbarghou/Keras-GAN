# Large amount of credit goes to:
# https://github.com/keras-team/keras-contrib/blob/master/examples/improved_wgan.py
# which I've used as a reference for this implementation

from __future__ import print_function, division

from functools import partial
import json
import os

import keras.backend as K
import matplotlib.pyplot as plt
import numpy as np
from keras.datasets import mnist
from keras.layers import BatchNormalization, Activation, ZeroPadding2D
from keras.layers import Input, Dense, Reshape, Flatten, Dropout
from keras.layers.advanced_activations import LeakyReLU
from keras.layers.convolutional import UpSampling2D, Conv2D
from keras.layers.merge import _Merge
from keras.models import model_from_json, Sequential, Model
from keras.optimizers import RMSprop

from keras_gan.gan_base import GANBase


class RandomWeightedAverage(_Merge):
    """Provides a (random) weighted average between real and generated image samples"""
    def _merge_function(self, inputs):
        alpha = K.random_uniform((32, 1, 1, 1))
        return (alpha * inputs[0]) + ((1 - alpha) * inputs[1])


def wasserstein_loss(y_true, y_pred):
    return K.mean(y_true * y_pred)


def gradient_penalty_loss(_, y_pred, averaged_samples):
    """
    Computes gradient penalty based on prediction and weighted real / fake samples
    """
    gradients = K.gradients(y_pred, averaged_samples)[0]
    # compute the euclidean norm by squaring ...
    gradients_sqr = K.square(gradients)
    #   ... summing over the rows ...
    gradients_sqr_sum = K.sum(gradients_sqr,
                              axis=np.arange(1, len(gradients_sqr.shape)))
    #   ... and sqrt
    gradient_l2_norm = K.sqrt(gradients_sqr_sum)
    # compute lambda * (1 - ||grad||)^2 still for each single sample
    gradient_penalty = K.square(1 - gradient_l2_norm)
    # return the mean as loss over all the batch samples
    return K.mean(gradient_penalty)


class ModelBuilder(object):
    def build_layers(self):
        raise NotImplemented

    def build(self):
        model = Sequential()
        self.build_layers(model)
        input_layer = Input(shape=(self.input_shape,))
        output_layer = model(input_layer)
        return Model(input_layer, output_layer)


class WGANGPGeneratorBuilder(ModelBuilder):
    def __init__(self,
                 input_shape,
                 initial_n_filters=128,
                 initial_height=7,
                 initial_width=7,
                 n_layer_filters=(128, 64),
                 channels=1):
        """Example usage:
            builder = WGANGPGeneratorBuilder()
            generator_model = builder.build()

        :param input_shape:
        :param initial_n_filters:
        :param initial_height:
        :param initial_width:
        :param n_layer_filters:
        :param channels:
        """
        self.input_shape = input_shape
        self.initial_n_filters = initial_n_filters
        self.initial_height = initial_height
        self.initial_width = initial_width
        self.n_layer_filters = n_layer_filters
        self.initial_layer_shape = (self.initial_height, self.initial_width, self.initial_n_filters)
        self.channels = channels

    def build_first_layer(self, model):
        model.add(Dense(np.prod(self.initial_layer_shape), activation="relu", input_dim=self.input_shape))
        model.add(Reshape(self.initial_layer_shape))

    def build_middle_layers(self, model):
        for n_filters in self.n_layer_filters:
            model.add(UpSampling2D())
            model.add(Conv2D(n_filters, kernel_size=4, padding="same"))
            model.add(BatchNormalization(momentum=0.8))
            model.add(Activation("relu"))

    def build_last_layer(self, model):
        model.add(Conv2D(self.channels, kernel_size=4, padding="same"))
        model.add(Activation("tanh"))

    def build_layers(self, model):
        self.build_first_layer(model)
        self.build_middle_layers(model)
        self.build_last_layer(model)


class WGANGPCriticBuilder(object):
    def __init__(self,
                 input_shape,
                 layer_configs=[(16, False, False),
                                (32, True, True),
                                (64, False, True),
                                (128, False, True)]):
        """Example usage:
            builder = WGANGPCriticBuilder()
            critic_model = builder.build()

        Note that configs take the form of a list of tuples, one for each layer.  The tuples
        are a tripple of (n_filters, use_zero_padding, use_batch_normalization)

        :param input_shape:
        :param configs a list of 3-tuples of (n_filters, use_zero_padding, use_batchnormaliation):
        """
        self.input_shape = input_shape
        self.layer_configs = layer_configs

    def build_layers(self, model):
        for idx, (n_filters, z_pad, batch_normalize) in enumerate(self.layer_configs):
            if idx == 0:
                model.add(Conv2D(n_filters, kernel_size=3, strides=2, padding="same", input_shape=self.input_shape))
            else:
                model.add(Conv2D(n_filters, kernel_size=3, strides=2, padding="same"))
            if z_pad:
                model.add(ZeroPadding2D(padding=((0, 1), (0, 1))))
            if batch_normalize:
                model.add(BatchNormalization(momentum=0.8))
            model.add(LeakyReLU(alpha=0.2))
            model.add(Dropout(0.25))

        model.add(Flatten())
        model.add(Dense(1))

    def build(self):
        model = Sequential()
        self.build_layers(model)

        img = Input(shape=self.input_shape)
        validity = model(img)

        return Model(img, validity)


class WGANGP(GANBase):
    def __init__(
            self,
            img_shape=[28, 28, 1],
            latent_dim=100,
            n_critic=5,
            optimizer=RMSprop(lr=0.00005),
            dataset=mnist,
            model_name='wgan_mnist',
            model_dir="models",
            *args,
            **kwargs):
        """
        Construct WGANGP GANBase.

        Simple MNIST Example:
            gan = WGANGP(...)
            gan.train(...)
            gan.generate()

        :param img_shape:
        :param latent_dim:
        :param n_critic:
        :param optimizer:
        :param dataset:
        :param model_name:
        :param model_dir:
        :param generator_builder:
        :param args:
        :param kwargs:
        """
        super(WGANGP, self).__init__(optimizer=optimizer, *args, **kwargs)

        self.img_shape = img_shape
        self.channels = self.img_shape[-1]
        self.latent_dim = latent_dim

        # Following parameter and optimizer set as recommended in paper
        self.n_critic = n_critic

        self.dataset = dataset
        self.model_name = model_name
        self.model_dir = model_dir

        self.epoch = 0

        self.generator_builder=WGANGPGeneratorBuilder(input_shape=latent_dim)
        self.critic_builder=WGANGPCriticBuilder(input_shape=img_shape)

        # Build the generator, critic, and computational graph
        self.generator = self.build_generator()
        self.critic = self.build_critic()
        self.critic_graph, self.generator_graph = self.build_computational_graphs()

    def build_computational_graphs(self):
        return self.build_critic_graph(), self.build_generator_graph()

    def build_critic_graph(self):
        """
        Construct Computational Graph for the Critic
        """

        # Freeze generator's layers while training critic
        self.generator.trainable = False

        # Image input (real sample)
        real_img = Input(shape=self.img_shape)

        # Noise input
        z_disc = Input(shape=(100,))
        # Generate image based of noise (fake sample)
        fake_img = self.generator(z_disc)

        # Discriminator determines validity of the real and fake images
        fake = self.critic(fake_img)
        valid = self.critic(real_img)

        # Construct weighted average between real and fake images
        interpolated_img = RandomWeightedAverage()([real_img, fake_img])
        # Determine validity of weighted sample
        validity_interpolated = self.critic(interpolated_img)

        # Use Python partial to provide loss function with additional
        # 'averaged_samples' argument
        partial_gp_loss = partial(gradient_penalty_loss, averaged_samples=interpolated_img)
        partial_gp_loss.__name__ = 'gradient_penalty'  # Keras requires function names

        critic_graph = Model(inputs=[real_img, z_disc], outputs=[valid, fake, validity_interpolated])
        critic_graph.compile(loss=[wasserstein_loss, wasserstein_loss, partial_gp_loss],
                             optimizer=self.get_optimizer(),
                             loss_weights=[1, 1, 10])
        return critic_graph

    def build_generator_graph(self):
        """
        Construct Computational Graph for Generator
        """
        # For the generator we freeze the critic's layers
        self.critic.trainable = False
        self.generator.trainable = True

        # Sampled noise for input to generator
        z_gen = Input(shape=(self.latent_dim,))
        # Generate images based of noise
        img = self.generator(z_gen)
        # Discriminator determines validity
        valid = self.critic(img)
        # Defines generator model
        generator_graph = Model(z_gen, valid)
        generator_graph.compile(loss=wasserstein_loss, optimizer=self.get_optimizer())
        return generator_graph

    def get_config(self):
        generator_path = self.get_generator_path()
        generator_json_path = self.get_generator_path(file_format="json")
        critic_path = self.get_critic_path()
        critic_json_path = self.get_critic_path(file_format="json")
        config_path = self.get_config_path()
        config = {
            "channels": self.channels,
            "img_shape": self.img_shape,
            "latent_dim": self.latent_dim,
            "n_critic": self.n_critic,
            "epoch": self.epoch,
            "generator_path": generator_path,
            "generator_json_path": generator_json_path,
            "critic_path": critic_path,
            "critic_json_path": critic_json_path,
            "config_path": config_path,
        }
        return config

    def set_config(self, config):
        self.channels = config["channels"]
        self.img_shape = config["img_shape"]
        self.latent_dim = config["latent_dim"]
        self.n_critic = config["n_critic"]
        self.epoch = config["epoch"]

    def get_config_path(self, suffix=None):
        if suffix:
            file_name = "{}_config_{}.json".format(self.model_name, suffix)
        else:
            file_name = "{}_config.json".format(self.model_name)
        file_path = os.path.join(self.model_dir, file_name)
        return file_path

    def get_generator_path(self, suffix=None, file_format="hdf5"):
        if suffix:
            file_name = "{}_generator_{}.{}".format(self.model_name, suffix, file_format)
        else:
            file_name = "{}_generator.{}".format(self.model_name, file_format)
        file_path = os.path.join(self.model_dir, file_name)
        return file_path

    def get_critic_path(self, suffix=None, file_format="hdf5"):
        if suffix:
            file_name = "{}_critic_{}.{}".format(self.model_name, suffix, file_format)
        else:
            file_name = "{}_critic.{}".format(self.model_name, file_format)
        file_path = os.path.join(self.model_dir, file_name)
        return file_path

    def save_generator(self, generator_path=None, generator_json_path=None):
        generator_path = generator_path or self.get_generator_path()
        self.generator.save(generator_path)
        generator_json_path = generator_json_path or self.get_generator_path(file_format="json")
        with open(generator_json_path, "w") as json_file:
            json_file.write(self.generator.to_json())
        return {
            "generator_path": generator_path,
            "generator_json_path": generator_json_path
        }

    def save_critic(self, critic_path=None, critic_json_path=None):
        critic_path = critic_path or self.get_critic_path()
        self.critic.save(critic_path)
        critic_json_path = critic_json_path or self.get_critic_path(file_format="json")
        with open(critic_json_path, "w") as json_file:
            json_file.write(self.critic.to_json())
        return {
            "critic_path": critic_path,
            "critic_json_path": critic_json_path
        }

    def save_config(self):
        config_path = self.get_config_path()
        config = self.get_config()
        with open(config_path, "w") as f:
            json.dump(config, f)
        return {
            "config_path": config_path
        }

    def save_model(self, generator_path=None, critic_path=None):
        paths = {}
        paths.update(**self.save_generator(generator_path))
        paths.update(**self.save_critic(critic_path))
        return paths

    def save(self):
        paths = {}
        paths.update(**self.save_config())
        paths.update(**self.save_model())
        return paths

    def load_generator(self, generator_path=None, generator_json_path=None):
        generator_json_path = generator_json_path or self.get_generator_path(file_format="json")
        with open(generator_json_path, "r") as json_file:
            self.generator = model_from_json(json_file.read())
        generator_path = generator_path or self.get_generator_path()
        self.generator.load_weights(generator_path)

    def load_critic(self, critic_path=None, critic_json_path=None):
        critic_json_path = critic_json_path or self.get_critic_path(file_format="json")
        with open(critic_json_path, "r") as json_file:
            self.critic = model_from_json(json_file.read())
        critic_path = critic_path or self.get_critic_path()
        self.critic.load_weights(critic_path)

    def load_config(self, config_path=None):
        config_path = config_path or self.get_config_path()
        with open(config_path, "r") as f:
            config = json.load(f)
        self.set_config(config)

    def load_model(self, generator_path=None, generator_json_path=None, critic_path=None, critic_json_path=None):
        self.load_generator(generator_path, generator_json_path)
        self.load_critic(critic_path, critic_json_path)

    @staticmethod
    def load(config_path,
             generator_path,
             generator_json_path,
             critic_path,
             critic_json_path):
        gan = WGANGP()
        gan.load_config(config_path)
        gan.load_model(generator_path, generator_json_path, critic_path, critic_json_path)
        return gan

    def build_generator(self):
        model = self.generator_builder.build()
        if self.verbose:
            model.summary()
        return model

    def build_critic(self):
        model = self.critic_builder.build()
        if self.verbose:
            model.summary()
        return model

    def generate_noise(self, batch_size):
        noise = np.random.normal(0, 1, (batch_size, self.latent_dim))
        return noise

    def generate_batch(self, batch_size):
        noise = self.generate_noise(batch_size)
        return self.generator.predict_on_batch(noise)

    def train_discriminator(self, x_train, batch_size):
        # Adversarial ground truths
        fake = np.ones((batch_size, 1))
        dummy = np.zeros((batch_size, 1))  # Dummy gt for gradient penalty

        valid = -np.ones((batch_size, 1))
        d_losses = []
        for _ in range(self.n_critic):
            # ---------------------
            #  Train Discriminator
            # ---------------------

            # Select a random batch of images
            idx = np.random.randint(0, x_train.shape[0], batch_size)
            imgs = x_train[idx]
            # Sample generator input
            noise = self.generate_noise(batch_size)
            # Train the critic
            d_loss = self.critic_graph.train_on_batch([imgs, noise],
                                                      [valid, fake, dummy])
            d_losses.append(d_loss)

        return d_losses

    def train_generator(self, batch_size):
        """
        Train Generator
        :param batch_size:
        :return g_loss:
        """

        valid = -np.ones((batch_size, 1))
        noise = self.generate_noise(batch_size)
        g_loss = self.generator_graph.train_on_batch(noise, valid)
        return g_loss

    def train(self, epochs, batch_size, sample_interval=50):

        # Load the dataset
        (_X_train, _), (_, _) = self.dataset.load_data()

        # Rescale -1 to 1
        _X_train = (_X_train.astype(np.float32) - 127.5) / 127.5
        _X_train = np.expand_dims(_X_train, axis=3)

        for self.epoch in range(self.epoch + 1, self.epoch + epochs + 1):

            d_losses = self.train_discriminator(_X_train, batch_size)
            g_loss = self.train_generator(batch_size)

            if self.verbose:
                # Plot the progress
                print("%d [D loss: %f] [G loss: %f]" % (self.epoch, d_losses[0][0], g_loss))

            if sample_interval and self.epoch % sample_interval == 0:
                self.sample_images()

    def sample_images(self, sample_image_filepath="./images"):
        r, c = 5, 5
        batch_size = r * c
        gen_imgs = self.generate_batch(batch_size)
        gen_imgs += np.min(gen_imgs)
        gen_imgs /= np.max(gen_imgs)

        fig, axs = plt.subplots(r, c)
        cnt = 0
        for i in range(r):
            for j in range(c):
                axs[i, j].imshow(gen_imgs[cnt, :, :, 0], cmap='gray')
                axs[i, j].axis('off')
                cnt += 1
        fig.savefig(
            os.path.join(
                sample_image_filepath,
                "sample_{:02d}.png".format(self.epoch)
            )
        )
        plt.close()


if __name__ == '__main__':
    wgan = WGANGP()
    wgan.train(epochs=30000, batch_size=32, sample_interval=100)
