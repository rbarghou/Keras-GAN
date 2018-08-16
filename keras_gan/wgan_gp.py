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
from keras.models import Sequential, Model
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


class WGANGP(GANBase):
    def __init__(
            self,
            img_shape=(28, 28, 1),
            latent_dim=100,
            n_critic=5,
            optimizer=RMSprop(lr=0.00005),
            dataset=mnist,
            model_name='wgan_mnist',
            model_dir="models",
            *args,
            **kwargs):
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
        critic_path = self.get_critic_path()
        config_path = self.get_config_path()
        config = {
            "channels": self.channels,
            "img_shape": self.img_shape,
            "latent_dim": self.latent_dim,
            "n_critic": self.n_critic,
            "epoch": self.epoch,
            "generator_path": generator_path,
            "critic_path": critic_path,
            "config_path": config_path,
        }
        return config

    def get_config_path(self, suffix=None):
        if suffix:
            file_name = "{}_config_{}.json".format(self.model_name, suffix)
        else:
            file_name = "{}_config.json".format(self.model_name)
        file_path = os.path.join(self.model_dir, file_name)
        return file_path

    def get_generator_path(self, suffix=None):
        if suffix:
            file_name = "{}_generator_{}.hdf5".format(self.model_name, suffix)
        else:
            file_name = "{}_generator.hdf5".format(self.model_name)
        file_path = os.path.join(self.model_dir, file_name)
        return file_path

    def get_critic_path(self, suffix=None):
        if suffix:
            file_name = "{}_critic_{}.hdf5".format(self.model_name, suffix)
        else:
            file_name = "{}_critic.hdf5".format(self.model_name)
        file_path = os.path.join(self.model_dir, file_name)
        return file_path

    def save_generator(self, generator_path=None):
        if generator_path is None:
            generator_path = self.get_generator_path()
        self.generator.save(generator_path)

    def save_critic(self, critic_path=None):
        if critic_path is None:
            critic_path = self.get_critic_path()
        self.critic.save(critic_path)

    def save_config(self):
        config_path = self.get_config_path()
        config = self.get_config()
        with open(config_path, "w") as f:
            json.dump(config, f)

    def save_model(self, generator_path=None, critic_path=None):
        generator_path = generator_path or self.get_generator_path()
        critic_path = critic_path or self.get_critic_path()
        self.save_generator(generator_path)
        self.save_critic(critic_path)

    def save(self):
        self.save_config()
        self.save_model()

    def build_generator(self):
        initial_n_filters = 128
        initial_height = 7
        initial_width = 7
        initial_layer_shape = (initial_height, initial_width, initial_n_filters)
        n_layer_filters = [128, 64]

        model = Sequential()

        model.add(Dense(np.prod(initial_layer_shape), activation="relu", input_dim=self.latent_dim))
        model.add(Reshape(initial_layer_shape))

        for n_filters in n_layer_filters:
            model.add(UpSampling2D())
            model.add(Conv2D(n_filters, kernel_size=4, padding="same"))
            model.add(BatchNormalization(momentum=0.8))
            model.add(Activation("relu"))

        model.add(Conv2D(self.channels, kernel_size=4, padding="same"))
        model.add(Activation("tanh"))

        if self.verbose:
            model.summary()

        noise = Input(shape=(self.latent_dim,))
        img = model(noise)

        return Model(noise, img)

    def build_critic(self):
        configs = [
            (16, False, False),
            (32, True, True),
            (64, False, True),
            (128, False, True),
        ]

        model = Sequential()

        for idx, (n_filters, z_pad, batch_normalize) in enumerate(configs):
            if idx == 0:
                model.add(Conv2D(n_filters, kernel_size=3, strides=2, padding="same", input_shape=self.img_shape))
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

        if self.verbose:
            model.summary()

        img = Input(shape=self.img_shape)
        validity = model(img)

        return Model(img, validity)

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
