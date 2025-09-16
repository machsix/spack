.. Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

.. Spack documentation master file, created by
   sphinx-quickstart on Mon Dec  9 15:32:41 2013.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.

.. meta::
   :description lang=en:
      Documentation of Spack, a flexible package manager for high-performance computing, designed to support multiple versions and configurations of software on a wide variety of platforms.

Spack
===================

Spack is a package management tool designed to support multiple versions and configurations of software on a wide variety of platforms and environments.
It was designed for large supercomputing centers, where many users and application teams share common installations of software on clusters with exotic architectures, using libraries that do not have a standard ABI.
Spack is non-destructive: installing a new version does not break existing installations, so many configurations can coexist on the same system.

Most importantly, Spack is *simple*.
It offers a simple *spec* syntax so that users can specify versions and configuration options concisely.
Spack is also simple for package authors: package files are written in pure Python, and specs allow package authors to maintain a single file for many different builds of the same package.

See the :doc:`features` for examples and highlights.

Get Spack from the `GitHub repository <https://github.com/spack/spack>`_ and install your first package:

.. code-block:: console

   $ git clone --depth=2 https://github.com/spack/spack.git
   $ cd spack/bin
   $ ./spack install libelf

.. note::
   ``--depth=2`` prunes the git history to reduce the size of the Spack installation.

If you're new to Spack and want to start using it, see :doc:`getting_started`, or refer to the full manual below.


.. toctree::
   :maxdepth: 2
   :caption: Introduction

   features
   getting_started
   spec_syntax
   installing_prerequisites
   windows

.. toctree::
   :maxdepth: 2
   :caption: Basic Usage

   package_fundamentals
   configuring_compilers
   environments_basics
   frequently_asked_questions
   getting_help

.. toctree::
   :maxdepth: 2
   :caption: Links

   Tutorial (spack-tutorial.rtfd.io) <https://spack-tutorial.readthedocs.io>
   Packages (packages.spack.io) <https://packages.spack.io>
   Binaries (binaries.spack.io) <https://cache.spack.io>

.. toctree::
   :maxdepth: 2
   :caption: Configuration

   configuration
   config_yaml
   packages_yaml
   toolchains_yaml
   build_settings
   repositories
   mirrors
   chain
   module_file_support
   include_yaml
   env_vars_yaml

.. toctree::
   :maxdepth: 2
   :caption: Reference

   environments
   containers
   binary_caches
   bootstrapping
   command_index
   extensions
   pipelines
   signing
   gpu_configuration

.. toctree::
   :maxdepth: 2
   :caption: Contributing

   packaging_guide_creation
   packaging_guide_build
   packaging_guide_testing
   packaging_guide_advanced
   build_systems
   contribution_guide
   developer_guide

.. toctree::
   :maxdepth: 2
   :caption: Advanced Topics

   advanced_topics

.. toctree::
   :maxdepth: 2
   :caption: API Docs

   Spack Package API <package_api>
   Spack Builtin Repo <spack_repo>
   Spack API Docs <spack>

Indices and tables
------------------

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
