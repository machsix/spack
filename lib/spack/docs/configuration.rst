.. Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

.. meta::
   :description lang=en:
      Learn how to configure Spack using its flexible YAML-based system. This guide covers the different configuration scopes and provides links to detailed documentation for each configuration file, helping you customize Spack to your specific needs.

.. _configuration:

Configuration Files
===================

Spack has many configuration files.
Here is a quick list of them, in case you want to skip directly to specific docs:

* :ref:`concretizer.yaml <concretizer-options>`
* :ref:`config.yaml <config-yaml>`
* :ref:`include.yaml <include-yaml>`
* :ref:`mirrors.yaml <mirrors>`
* :ref:`modules.yaml <modules>`
* :ref:`packages.yaml <packages-config>` (including :ref:`compiler configuration <compiler-config>`)
* :ref:`repos.yaml <repositories>`
* :ref:`toolchains.yaml <toolchains>`

You can also add any of these as inline configuration in the YAML manifest file (``spack.yaml``) describing an :ref:`environment <environment-configuration>`.

YAML Format
-----------

Spack configuration files are written in YAML.
We chose YAML because it's human-readable but also versatile in that it supports dictionaries, lists, and nested sections.
For more details on the format, see `yaml.org <https://yaml.org>`_.
Here is an example ``config.yaml`` file:

.. code-block:: yaml

   config:
     install_tree:
       root: $spack/opt/spack
     build_stage:
     - $tempdir/$user/spack-stage
     - ~/.spack/stage

Each Spack configuration file is nested under a top-level section corresponding to its name.
So, ``config.yaml`` starts with ``config:``, ``mirrors.yaml`` starts with ``mirrors:``, etc.

.. tip::

   Validation and autocompletion of Spack config files can be enabled in your editor using `JSON Schema Store <https://www.schemastore.org/>`_.

.. _configuration-scopes:

Configuration Scopes
--------------------

Spack pulls configuration data from files in several directories.
There are multiple configuration scopes.
From lowest to highest precedence:

#. **defaults**: Stored in ``$(prefix)/etc/spack/defaults/``.
   These are the "factory" settings.
   Users should generally not modify the settings here, but should override them in other configuration scopes.
   The defaults here will change from version to version of Spack.

#. **system**: Stored in ``/etc/spack/``.
   These are settings for this machine or for all machines on which this file system is mounted.
   The system scope can be used for settings idiosyncratic to a particular machine, such as the locations of compilers or external packages.
   These settings are presumably controlled by someone with root access on the machine.
   They override the defaults scope.

#. **site**: Stored in ``$(prefix)/etc/spack/``.
   Settings here affect only *this instance* of Spack, and they override the defaults and system scopes.
   The site scope can be used for per-project settings (one Spack instance per project) or for site-wide settings on a multi-user machine (e.g., for a common Spack instance).

#. **plugin**: Read from a Python package's entry points.
   Settings here affect all instances of Spack running with the same Python installation.
   This scope takes higher precedence than site, system, and default scopes.

#. **user**: Stored in the home directory: ``~/.spack/``.
   These settings affect all instances of Spack and take higher precedence than site, system, plugin, or defaults scopes.

#. **custom**: Stored in a custom directory specified by ``--config-scope``.
   If multiple scopes are listed on the command line, they are ordered from lowest to highest precedence.

#. **environment**: When using Spack :ref:`environments`, Spack reads additional configuration from the environment file.
   See :ref:`environment-configuration` for further details on these scopes.
   Environment scopes can be referenced from the command line as ``env:name`` (e.g., to reference environment ``foo``, use ``env:foo``).

#. **command line**: Build settings specified on the command line take precedence over all other scopes.

Each configuration directory may contain several configuration files, such as ``config.yaml``, ``packages.yaml``, or ``mirrors.yaml``.
When configurations conflict, settings from higher-precedence scopes override lower-precedence settings.

Commands that modify scopes (e.g., ``spack compilers``, ``spack repo``, etc.) take a ``--scope=<name>`` parameter that you can use to control which scope is modified.
By default, they modify the highest-precedence available scope that is not read-only (like `defaults`).

.. _custom-scopes:

Custom scopes
^^^^^^^^^^^^^

In addition to the ``defaults``, ``system``, ``site``, and ``user`` scopes, you may add configuration scopes directly on the command line with the ``--config-scope`` argument, or ``-C`` for short.

For example, the following adds two configuration scopes, named ``scope-a`` and ``scope-b``, to a ``spack spec`` command:

.. code-block:: spec

   $ spack -C ~/myscopes/scope-a -C ~/myscopes/scope-b spec ncurses

Custom scopes come *after* the ``spack`` command and *before* the subcommand, and they specify a single path to a directory containing configuration files.
You can add the same configuration files to that directory that you can add to any other scope (e.g., ``config.yaml``, ``packages.yaml``, etc.).

If multiple scopes are provided:

#. Each must be preceded with the ``--config-scope`` or ``-C`` flag.
#. They must be ordered from lowest to highest precedence.

Example: scopes for release and development
"""""""""""""""""""""""""""""""""""""""""""

Suppose that you need to support simultaneous building of release and development versions of ``mypackage``, where ``mypackage`` depends on ``pkg-a``, which in turn depends on ``pkg-b``.
You could create the following files:

.. code-block:: yaml
   :caption: ``~/myscopes/release/packages.yaml``
   :name: code-example-release-packages-yaml

   packages:
     mypackage:
       prefer: ["@1.7"]
     pkg-a:
       prefer: ["@2.3"]
     pkg-b:
       prefer: ["@0.8"]

.. code-block:: yaml
   :caption: ``~/myscopes/develop/packages.yaml``
   :name: code-example-develop-packages-yaml

   packages:
     mypackage:
       prefer: ["@develop"]
     pkg-a:
       prefer: ["@develop"]
     pkg-b:
       prefer: ["@develop"]

You can switch between ``release`` and ``develop`` configurations using configuration arguments.
You would type ``spack -C ~/myscopes/release`` when you want to build the designated release versions of ``mypackage``, ``pkg-a``, and ``pkg-b``, and you would type ``spack -C ~/myscopes/develop`` when you want to build all of these packages at the ``develop`` version.

Example: swapping MPI providers
"""""""""""""""""""""""""""""""

Suppose that you need to build two software packages, ``pkg-a`` and ``pkg-b``.
For ``pkg-b`` you want a newer Python version and a different MPI implementation than for ``pkg-a``.
You can create different configuration scopes for use with ``pkg-a`` and ``pkg-b``:

.. code-block:: yaml
   :caption: ``~/myscopes/pkg-a/packages.yaml``
   :name: code-example-pkg-a-packages-yaml

   packages:
     python:
       require: ["@3.11"]
     mpi:
       require: [openmpi]

.. code-block:: yaml
   :caption: ``~/myscopes/pkg-b/packages.yaml``
   :name: code-example-pkg-b-packages-yaml

   packages:
     python:
       require: ["@3.13"]
     mpi:
       require: [mpich]


.. _plugin-scopes:

Plugin scopes
^^^^^^^^^^^^^

.. note::
   Python version >= 3.8 is required to enable plugin configuration.

Spack can be made aware of configuration scopes that are installed as part of a Python package.
To do so, register a function that returns the scope's path to the ``"spack.config"`` entry point.
Consider the Python package ``my_package`` that includes Spack configurations:

.. code-block:: console

  my-package/
  ├── src
  │   ├── my_package
  │   │   ├── __init__.py
  │   │   └── spack/
  │   │   │   └── config.yaml
  └── pyproject.toml

Adding the following to ``my_package``'s ``pyproject.toml`` will make ``my_package``'s ``spack/`` configurations visible to Spack when ``my_package`` is installed:

.. code-block:: toml

   [project.entry_points."spack.config"]
   my_package = "my_package:get_config_path"

The function ``my_package.get_config_path`` (matching the entry point definition) in ``my_package/__init__.py`` might look like:

.. code-block:: python

   import importlib.resources


   def get_config_path():
       dirname = importlib.resources.files("my_package").joinpath("spack")
       if dirname.exists():
           return str(dirname)

.. _platform-scopes:

Platform-specific Configuration
-------------------------------

.. warning::

   Prior to v1.0, each scope above -- except environment scopes -- had a corresponding platform-specific scope (e.g., ``defaults/linux``, ``system/windows``).
   This can now be accomplished through a suitably placed :ref:`include.yaml <include-yaml>` file.

There is often a need for platform-specific configuration settings.
For example, on most platforms, GCC is the preferred compiler.
However, on macOS (darwin), Clang often works for more packages, and is set as the default compiler.
This configuration is set in ``$(prefix)/etc/spack/defaults/darwin/packages.yaml``, which is included by ``$(prefix)/etc/spack/defaults/include.yaml``.
Since it is an included configuration of the ``defaults`` scope, settings in the ``defaults`` scope will take precedence.
You can override the values by specifying settings in ``system``, ``site``, ``user``, or ``custom``, where scope precedence is:

#. ``defaults``
#. ``system``
#. ``site``
#. ``user``
#. ``custom``

and settings in each scope taking precedence over those found in configuration files listed in the corresponding ``include.yaml`` files.

For example, if ``$(prefix)/etc/spack/defaults/include.yaml`` contains:

.. code-block:: yaml

   include:
   - path: "${platform}"
     optional: true

then, on macOS (``darwin``), configuration settings for files under the ``$(prefix)/etc/spack/defaults/darwin`` directory would be picked up.

.. note::

   You can get the name to use for ``<platform>`` by running ``spack arch --platform``.

Platform-specific configuration files can similarly be set up for the ``system``, ``site``, and ``user`` scopes by creating an ``include.yaml`` similar to the one above for ``defaults`` -- under the appropriate configuration paths (see :ref:`config-overrides`) and creating a subdirectory with the platform name that contains the configuration files.

.. note::

   Site-specific settings are located in configuration files under the ``$(prefix)/etc/spack/`` directory.

.. _config-scope-precedence:

Scope Precedence
----------------

When Spack queries for configuration parameters, it searches in higher-precedence scopes first.
So, settings in a higher-precedence file can override those with the same key in a lower-precedence one.
For list-valued settings, Spack merges lists by *prepending* items from higher-precedence configurations to items from lower-precedence configurations by default.
Completely ignoring lower-precedence configuration options is supported with the ``::`` notation for keys (see :ref:`config-overrides` below).

.. note::

   Settings in a scope take precedence over those provided in any included configuration files (i.e., files listed in :ref:`include.yaml <include-yaml>` or an ``include:`` section in ``spack.yaml``).

There are also special notations for string concatenation and precedence override:

* ``+:`` will force *prepending* strings or lists.
  For lists, this is the default behavior.
* ``-:`` works similarly, but for *appending* values.

See :ref:`config-prepend-append` for more details.

Simple keys
^^^^^^^^^^^

Let's look at an example of overriding a single key in a Spack configuration file.
If your configurations look like this:

.. code-block:: yaml
   :caption: ``$(prefix)/etc/spack/defaults/config.yaml``
   :name: code-example-defaults-config-yaml

   config:
     install_tree:
       root: $spack/opt/spack
     build_stage:
     - $tempdir/$user/spack-stage
     - ~/.spack/stage


.. code-block:: yaml
   :caption: ``~/.spack/config.yaml``
   :name: code-example-user-config-yaml

   config:
     install_tree:
       root: /some/other/directory


Spack will only override ``install_tree`` in the ``config`` section, and will take the site preferences for other settings.
You can see the final, combined configuration with the ``spack config get <configtype>`` command:

.. code-block:: console
   :emphasize-lines: 3

   $ spack config get config
   config:
     install_tree:
       root: /some/other/directory
     build_stage:
     - $tempdir/$user/spack-stage
     - ~/.spack/stage


.. _config-prepend-append:

String Concatenation
^^^^^^^^^^^^^^^^^^^^

Above, the user ``config.yaml`` *completely* overrides specific settings in the default ``config.yaml``.
Sometimes, it is useful to add a suffix/prefix to a path or name.
To do this, you can use the ``-:`` notation for *append* string concatenation at the end of a key in a configuration file.
For example:

.. code-block:: yaml
   :emphasize-lines: 1
   :caption: ``~/.spack/config.yaml``
   :name: code-example-append-install-tree

   config:
     install_tree:
       root-: /my/custom/suffix/

Spack will then append to the lower-precedence configuration under the ``root`` key:

.. code-block:: console

   $ spack config get config
   config:
     install_tree:
       root: /some/other/directory/my/custom/suffix
     build_stage:
     - $tempdir/$user/spack-stage
     - ~/.spack/stage


Similarly, ``+:`` can be used to *prepend* to a path or name:

.. code-block:: yaml
   :emphasize-lines: 1
   :caption: ``~/.spack/config.yaml``
   :name: code-example-prepend-install-tree

   config:
     install_tree:
       root+: /my/custom/suffix/


.. _config-overrides:

Overriding entire sections
^^^^^^^^^^^^^^^^^^^^^^^^^^

Above, the user ``config.yaml`` only overrides specific settings in the default ``config.yaml``.
Sometimes, it is useful to *completely* override lower-precedence settings.
To do this, you can use *two* colons at the end of a key in a configuration file.
For example:

.. code-block:: yaml
   :emphasize-lines: 1
   :caption: ``~/.spack/config.yaml``
   :name: code-example-override-config-section

   config::
     install_tree:
       root: /some/other/directory

Spack will ignore all lower-precedence configuration under the ``config::`` section:

.. code-block:: console

   $ spack config get config
   config:
     install_tree:
       root: /some/other/directory


List-valued settings
^^^^^^^^^^^^^^^^^^^^

Let's revisit the ``config.yaml`` example one more time.
The ``build_stage`` setting's value is an ordered list of directories:

.. code-block:: yaml
   :caption: ``$(prefix)/etc/spack/defaults/config.yaml``
   :name: code-example-defaults-build-stage

   config:
     build_stage:
     - $tempdir/$user/spack-stage
     - ~/.spack/stage


Suppose the user configuration adds its *own* list of ``build_stage`` paths:

.. code-block:: yaml
   :caption: ``~/.spack/config.yaml``
   :name: code-example-user-build-stage

   config:
     build_stage:
     - /lustre-scratch/$user/spack
     - ~/mystage


Spack will first look at the paths in the defaults ``config.yaml``, then the paths in the user's ``~/.spack/config.yaml``.
The list in the higher-precedence scope is *prepended* to the defaults.
``spack config get config`` shows the result:

.. code-block:: console
   :emphasize-lines: 5-8

   $ spack config get config
   config:
     install_tree:
       root: /some/other/directory
     build_stage:
     - /lustre-scratch/$user/spack
     - ~/mystage
     - $tempdir/$user/spack-stage
     - ~/.spack/stage


As in :ref:`config-overrides`, the higher-precedence scope can *completely* override the lower-precedence scope using ``::``.
So if the user config looked like this:

.. code-block:: yaml
   :emphasize-lines: 1
   :caption: ``~/.spack/config.yaml``
   :name: code-example-override-build-stage

   config:
     build_stage::
     - /lustre-scratch/$user/spack
     - ~/mystage


The merged configuration would look like this:

.. code-block:: console
   :emphasize-lines: 5-6

   $ spack config get config
   config:
     install_tree:
       root: /some/other/directory
     build_stage:
       - /lustre-scratch/$user/spack
       - ~/mystage


.. _config-file-variables:

Config File Variables
---------------------

Spack understands several variables which can be used in config file paths wherever they appear.
There are three sets of these variables: Spack-specific variables, environment variables, and user path variables.
Spack-specific variables and environment variables are both indicated by prefixing the variable name with ``$``.
User path variables are indicated at the start of the path with ``~`` or ``~user``.

Spack-specific variables
^^^^^^^^^^^^^^^^^^^^^^^^

Spack understands over a dozen special variables.
These are:

* ``$env``: name of the currently active :ref:`environment <environments>`
* ``$spack``: path to the prefix of this Spack installation
* ``$tempdir``: default system temporary directory (as specified in Python's `tempfile.tempdir <https://docs.python.org/2/library/tempfile.html#tempfile.tempdir>`_ variable.
* ``$user``: name of the current user
* ``$user_cache_path``: user cache directory (``~/.spack`` unless :ref:`overridden <local-config-overrides>`)
* ``$architecture``: the architecture triple of the current host, as detected by Spack.
* ``$arch``: alias for ``$architecture``.
* ``$platform``: the platform of the current host, as detected by Spack.
* ``$operating_system``: the operating system of the current host, as detected by the ``distro`` Python module.
* ``$os``: alias for ``$operating_system``.
* ``$target``: the ISA target for the current host, as detected by ArchSpec.
  E.g.
  ``skylake`` or ``neoverse-n1``.
* ``$target_family``.
  The target family for the current host, as detected by ArchSpec.
  E.g.
  ``x86_64`` or ``aarch64``.
* ``$date``: the current date in the format YYYY-MM-DD
* ``$spack_short_version``: the Spack version truncated to the first components.


Note that, as with shell variables, you can write these as ``$varname`` or with braces to distinguish the variable from surrounding characters: ``${varname}``.
Their names are also case insensitive, meaning that ``$SPACK`` works just as well as ``$spack``.
These special variables are substituted first, so any environment variables with the same name will not be used.

Environment variables
^^^^^^^^^^^^^^^^^^^^^

After Spack-specific variables are evaluated, environment variables are expanded.
These are formatted like Spack-specific variables, e.g., ``${varname}``.
You can use this to insert environment variables in your Spack configuration.

User home directories
^^^^^^^^^^^^^^^^^^^^^

Spack performs Unix-style tilde expansion on paths in configuration files.
This means that tilde (``~``) will expand to the current user's home directory, and ``~user`` will expand to a specified user's home directory.
The ``~`` must appear at the beginning of the path, or Spack will not expand it.

.. _configuration_environment_variables:

Environment Modifications
-------------------------

Spack allows users to prescribe custom environment modifications in a few places within its configuration files.
Every time these modifications are allowed, they are specified as a dictionary, like in the following example:

.. code-block:: yaml

   environment:
     set:
       LICENSE_FILE: "/path/to/license"
     unset:
     - CPATH
     - LIBRARY_PATH
     append_path:
       PATH: "/new/bin/dir"

The possible actions that are permitted are ``set``, ``unset``, ``append_path``, ``prepend_path``, and finally ``remove_path``.
They all require a dictionary of variable names mapped to the values used for the modification, with the exception of ``unset``, which requires just a list of variable names.
No particular order is ensured for the execution of each of these modifications.

Seeing Spack's Configuration
----------------------------

With so many scopes overriding each other, it can sometimes be difficult to understand what Spack's final configuration looks like.

Spack provides two useful ways to view the final "merged" version of any configuration file: ``spack config get`` and ``spack config blame``.

.. _cmd-spack-config-get:

``spack config get``
^^^^^^^^^^^^^^^^^^^^

``spack config get`` shows a fully merged configuration file, taking into account all scopes.
For example, to see the fully merged ``config.yaml``, you can type:

.. code-block:: console

   $ spack config get config
   config:
     debug: false
     checksum: true
     verify_ssl: true
     dirty: false
     build_jobs: 8
     install_tree:
       root: $spack/opt/spack
     template_dirs:
     - $spack/templates
     directory_layout: {architecture}/{compiler.name}-{compiler.version}/{name}-{version}-{hash}
     build_stage:
     - $tempdir/$user/spack-stage
     - ~/.spack/stage
     - $spack/var/spack/stage
     source_cache: $spack/var/spack/cache
     misc_cache: ~/.spack/cache
     locks: true

Likewise, this will show the fully merged ``packages.yaml``:

.. code-block:: console

   $ spack config get packages

You can use this in conjunction with the ``-C`` / ``--config-scope`` argument to see how your scope will affect Spack's configuration:

.. code-block:: console

   $ spack -C /path/to/my/scope config get packages


.. _cmd-spack-config-blame:

``spack config blame``
^^^^^^^^^^^^^^^^^^^^^^

``spack config blame`` functions much like ``spack config get``, but it shows exactly which configuration file each setting came from.
If you do not know why Spack is behaving a certain way, this command can help you track down the source of the configuration:

.. code-block:: console

   $ spack --insecure -C ./my-scope -C ./my-scope-2 config blame config
   ==> Warning: You asked for --insecure. Will NOT check SSL certificates.
   ---                                                   config:
   _builtin                                                debug: False
   /home/myuser/spack/etc/spack/defaults/config.yaml:72    checksum: True
   command_line                                            verify_ssl: False
   ./my-scope-2/config.yaml:2                              dirty: False
   _builtin                                                build_jobs: 8
   ./my-scope/config.yaml:2                                install_tree: /path/to/some/tree
   /home/myuser/spack/etc/spack/defaults/config.yaml:23    template_dirs:
   /home/myuser/spack/etc/spack/defaults/config.yaml:24    - $spack/templates
   /home/myuser/spack/etc/spack/defaults/config.yaml:28    directory_layout: {architecture}/{compiler.name}-{compiler.version}/{name}-{version}-{hash}
   /home/myuser/spack/etc/spack/defaults/config.yaml:49    build_stage:
   /home/myuser/spack/etc/spack/defaults/config.yaml:50    - $tempdir/$user/spack-stage
   /home/myuser/spack/etc/spack/defaults/config.yaml:51    - ~/.spack/stage
   /home/myuser/spack/etc/spack/defaults/config.yaml:52    - $spack/var/spack/stage
   /home/myuser/spack/etc/spack/defaults/config.yaml:57    source_cache: $spack/var/spack/cache
   /home/myuser/spack/etc/spack/defaults/config.yaml:62    misc_cache: ~/.spack/cache
   /home/myuser/spack/etc/spack/defaults/config.yaml:86    locks: True

You can see above that the ``build_jobs`` and ``debug`` settings are built-in and are not overridden by a configuration file.
The ``verify_ssl`` setting comes from the ``--insecure`` option on the command line.
The ``dirty`` and ``install_tree`` settings come from the custom scopes ``./my-scope`` and ``./my-scope-2``, and all other configuration options come from the default configuration files that ship with Spack.

.. _local-config-overrides:

Overriding Local Configuration
------------------------------

Spack's ``system`` and ``user`` scopes provide ways for administrators and users to set global defaults for all Spack instances, but for use cases where one wants a clean Spack installation, these scopes can be undesirable.
For example, users may want to opt out of global system configuration, or they may want to ignore their own home directory settings when running in a continuous integration environment.

Spack also, by default, keeps various caches and user data in ``~/.spack``, but users may want to override these locations.

Spack provides three environment variables that allow you to override or opt out of configuration locations:

* ``SPACK_USER_CONFIG_PATH``: Override the path to use for the ``user`` scope (``~/.spack`` by default).
* ``SPACK_SYSTEM_CONFIG_PATH``: Override the path to use for the ``system`` scope (``/etc/spack`` by default).
* ``SPACK_DISABLE_LOCAL_CONFIG``: Set this environment variable to completely disable **both** the system and user configuration directories.
  Spack will then only consider its own defaults and ``site`` configuration locations.

And one that allows you to move the default cache location:

* ``SPACK_USER_CACHE_PATH``: Override the default path to use for user data (misc_cache, tests, reports, etc.)

With these settings, if you want to isolate Spack in a CI environment, you can do this:

.. code-block:: console

  $ export SPACK_DISABLE_LOCAL_CONFIG=true
  $ export SPACK_USER_CACHE_PATH=/tmp/spack
