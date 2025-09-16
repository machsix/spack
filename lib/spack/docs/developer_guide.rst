.. Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

.. meta::
   :description lang=en:
      A comprehensive guide for developers working on Spack itself, covering the directory structure, code organization, and key concepts like specs and packages.

.. _developer_guide:

Developer Guide
===============

This guide is intended for people who want to work on Spack itself.
If you just want to develop packages, see the :doc:`Packaging Guide <packaging_guide_creation>`.

It is assumed that you have read the :ref:`basic-usage` and :doc:`packaging guide <packaging_guide_creation>` sections and that you are familiar with the concepts discussed there.

Overview
--------

Spack is designed with three separate roles in mind:

#. **Users**, who need to install software *without* knowing all the details about how it is built.
#. **Packagers**, who know how a particular software package is built and encode this information in package files.
#. **Developers**, who work on Spack, add new features, and try to make the jobs of packagers and users easier.

Users could be end-users installing software in their home directory or administrators installing software to a shared directory on a shared machine.
Packagers could be administrators who want to automate software builds or application developers who want to make their software more accessible to users.

As you might expect, there are many types of users with different levels of sophistication, and Spack is designed to accommodate both simple and complex use cases for packages.
A user who only knows that they need a certain package should be able to type something simple, like ``spack install <package name>``, and get the package that they want.
If a user wants to ask for a specific version, use particular compilers, or build several versions with different configurations, then that should be possible with a minimal amount of additional specification.

This gets us to the two key concepts in Spack's software design:

#. **Specs**: expressions for describing builds of software, and
#. **Packages**: Python modules that build software according to a spec.

A package is a template for building particular software, and a spec is a descriptor for one or more instances of that template.
Users express the configuration they want using a spec, and a package turns the spec into a complete build.

The obvious difficulty with this design is that users underspecify what they want.
To build a software package, the package object needs a *complete* specification.
In Spack, if a spec describes only one instance of a package, then we say it is **concrete**.
If a spec could describe many instances (i.e., it is underspecified in one way or another), then we say it is **abstract**.

Spack's job is to take an *abstract* spec from the user, find a *concrete* spec that satisfies the constraints, and hand the task of building the software off to the package object.

Packages are managed through Spack's **package repositories**, which allow packages to be stored in multiple repositories with different namespaces.
The built-in packages are hosted in a separate Git repository and automatically managed by Spack, while custom repositories can be added for organization-specific or experimental packages.

The rest of this document describes all the pieces that come together to make that happen.

Directory Structure
-------------------

So that you can familiarize yourself with the project, we will start with a high-level view of Spack's directory structure:

.. code-block:: none

   spack/                  <- installation root
      bin/
         spack             <- main spack executable

      etc/
         spack/            <- Spack config files.
                              Can be overridden by files in ~/.spack.

      var/
         spack/
             test_repos/   <- contains package repositories for tests
             cache/        <- saves resources downloaded during installs

      opt/
         spack/            <- packages are installed here

      lib/
         spack/
            docs/          <- source for this documentation

            external/      <- external libs included in Spack distribution

            spack/                <- spack module; contains Python code
               build_systems/     <- modules for different build systems
               cmd/               <- each file in here is a Spack subcommand
               compilers/         <- compiler description files
               container/         <- module for spack containerize
               hooks/             <- hook modules to run at different points
               modules/           <- modules for Lmod, Tcl, etc.
               operating_systems/ <- operating system modules
               platforms/         <- different Spack platforms
               reporters/         <- reporters like CDash, JUnit
               schema/            <- schemas to validate data structures
               solver/            <- the Spack solver
               test/              <- unit test modules
               util/              <- common code

Spack is designed so that it could live within a `standard UNIX directory hierarchy <http://linux.die.net/man/7/hier>`_, so ``lib``, ``var``, and ``opt`` all contain a ``spack`` subdirectory in case Spack is installed alongside other software.
Most of the interesting parts of Spack live in ``lib/spack``.

.. note::

   **Package Repositories**: Built-in packages are hosted in a separate Git repository at `spack/spack-packages <https://github.com/spack/spack-packages>`_ and are automatically cloned to ``~/.spack/package_repos/`` when needed.
   The ``var/spack/test_repos/`` directory is used for unit tests only.
   See :ref:`repositories` for details on package repositories.

Spack has *one* directory layout, and there is no installation process.
Most Python programs do not look like this (they use ``distutils``, ``setup.py``, etc.), but we wanted to make Spack *very* easy to use.
The simple layout spares users from the need to install Spack into a Python environment.
Many users do not have write access to a Python installation, and installing an entire new instance of Python to bootstrap Spack would be very complicated.
Users should not have to install a big, complicated package to use the thing that is supposed to spare them from the details of big, complicated packages.
The end result is that Spack works out of the box: clone it and add ``bin`` to your ``PATH``, and you are ready to go.

Code Structure
--------------

This section gives an overview of the various Python modules in Spack, grouped by functionality.

Package-related modules
^^^^^^^^^^^^^^^^^^^^^^^

:mod:`spack.package_base`
  Contains the :class:`~spack.package_base.PackageBase` class, which is the superclass for all packages in Spack.

:mod:`spack.util.naming`
  Contains functions for mapping between Spack package names, Python module names, and Python class names.

:mod:`spack.directives`
  *Directives* are functions that can be called inside a package definition to modify the package, like :func:`~spack.directives.depends_on` and :func:`~spack.directives.provides`.
  See :ref:`dependencies` and :ref:`virtual-dependencies`.

:mod:`spack.multimethod`
  Implementation of the :func:`@when <spack.multimethod.when>` decorator, which allows :ref:`multimethods <multimethods>` in packages.

Spec-related modules
^^^^^^^^^^^^^^^^^^^^

:mod:`spack.spec`
  Contains :class:`~spack.spec.Spec`.
  Also implements most of the logic for concretization of specs.

:mod:`spack.spec_parser`
  Contains :class:`~spack.spec_parser.SpecParser` and functions related to parsing specs.

:mod:`spack.version`
  Implements a simple :class:`~spack.version.Version` class with simple comparison semantics.
  It also implements :class:`~spack.version.VersionRange` and :class:`~spack.version.VersionList`.
  All three are comparable with each other and offer union and intersection operations.
  Spack uses these classes to compare versions and to manage version constraints on specs.
  Comparison semantics are similar to the ``LooseVersion`` class in ``distutils`` and to the way RPM compares version strings.

:mod:`spack.compilers`
  Submodules contains descriptors for all valid compilers in Spack.
  This is used by the build system to set up the build environment.

  .. warning::

     Not yet implemented.
     Currently has two compiler descriptions, but compilers aren't fully integrated with the build process yet.

Build environment
^^^^^^^^^^^^^^^^^

:mod:`spack.stage`
  Handles creating temporary directories for builds.

:mod:`spack.build_environment`
  This contains utility functions used by the compiler wrapper script, ``cc``.

:mod:`spack.directory_layout`
  Classes that control the way an installation directory is laid out.
  Create more implementations of this to change the hierarchy and naming scheme in ``$spack_prefix/opt``

Spack Subcommands
^^^^^^^^^^^^^^^^^

:mod:`spack.cmd`
  Each module in this package implements a Spack subcommand.
  See :ref:`writing commands <writing-commands>` for details.

Unit tests
^^^^^^^^^^

``spack.test``
  Implements Spack's test suite.
  Add a module and put its name in the test suite in ``__init__.py`` to add more unit tests.


Other Modules
^^^^^^^^^^^^^

:mod:`spack.url`
  URL parsing, for deducing names and versions of packages from tarball URLs.

:mod:`spack.error`
  :class:`~spack.error.SpackError`, the base class for Spack's exception hierarchy.

:mod:`spack.llnl.util.tty`
  Basic output functions for all of the messages Spack writes to the terminal.

:mod:`spack.llnl.util.tty.color`
  Implements a color formatting syntax used by ``spack.tty``.

:mod:`spack.llnl.util`
  In this package are a number of utility modules for the rest of Spack.

.. _package-repositories:

Package Repositories
^^^^^^^^^^^^^^^^^^^^

Spack's package repositories allow developers to manage packages from multiple sources.
Understanding this system is important for developing Spack itself.

:mod:`spack.repo`
  The core module for managing package repositories.
  Contains the ``Repo`` and ``RepoPath`` classes that handle loading and searching packages from multiple repositories.

Built-in packages are stored in a separate Git repository (`spack/spack-packages <https://github.com/spack/spack-packages>`_) rather than being included directly in the Spack source tree.
This repository is automatically cloned to ``~/.spack/package_repos/`` when needed.

Key concepts:

* **Repository namespaces**: Each repository has a unique namespace (e.g., ``builtin``)
* **Repository search order**: Packages are found by searching repositories in order
* **Git-based repositories**: Remote repositories can be automatically cloned and managed
* **Repository configuration**: Managed through ``repos.yaml`` configuration files

See :ref:`repositories` for complete details on configuring and managing package repositories.

.. _package_class_structure:

Package class architecture
--------------------------

.. note::

   This section aims to provide a high-level knowledge of how the package class architecture evolved in Spack, and provides some insights on the current design.

Packages in Spack were originally designed to support only a single build system.
The overall class structure for a package looked like:

.. image:: images/original_package_architecture.png
   :scale: 60 %
   :align: center

In this architecture the base class ``AutotoolsPackage`` was responsible for both the metadata related to the ``autotools`` build system (e.g. dependencies or variants common to all packages using it), and for encoding the default installation procedure.

In reality, a non-negligible number of packages are either changing their build system during the evolution of the project, or using different build systems for different platforms.
An architecture based on a single class requires hacks or other workarounds to deal with these cases.

To support a model more adherent to reality, Spack v0.19 changed its internal design by extracting the attributes and methods related to building a software into a separate hierarchy:

.. image:: images/builder_package_architecture.png
   :scale: 60 %
   :align: center

In this new format each ``package.py`` contains one ``*Package`` class that gathers all the metadata, and one or more ``*Builder`` classes that encode the installation procedure.
A specific builder object is created just before the software is built, so at a time where Spack knows which build system needs to be used for the current installation, and receives a ``package`` object during initialization.

Compatibility with single-class format
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Internally, Spack always uses builders to perform operations related to the installation of a specific software.
The builders are created in the ``spack.builder.create`` function.

.. literalinclude:: _spack_root/lib/spack/spack/builder.py
   :pyobject: create

To achieve backward compatibility with the single-class format Spack creates in this function a special "adapter builder", if no custom builder is detected in the recipe:

.. image:: images/adapter.png
   :scale: 60 %
   :align: center

Overall the role of the adapter is to route access to attributes of methods first through the ``*Package`` hierarchy, and then back to the base class builder.
This is schematically shown in the diagram above, where the adapter role is to "emulate" a method resolution order like the one represented by the red arrows.


.. _writing-commands:

Writing commands
----------------

Adding a new command to Spack is easy.
Simply add a ``<name>.py`` file to ``lib/spack/spack/cmd/``, where ``<name>`` is the name of the subcommand.
At a bare minimum, two functions are required in this file:

``setup_parser()``
^^^^^^^^^^^^^^^^^^

Unless your command does not accept any arguments, a ``setup_parser()`` function is required to define what arguments and flags your command takes.
See the `Argparse documentation <https://docs.python.org/3/library/argparse.html>`_ for more details on how to add arguments.

Some commands have a set of subcommands, like ``spack compiler find`` or ``spack module lmod refresh``.
You can add subparsers to your parser to handle this.
Check out ``spack edit --command compiler`` for an example of this.

Many commands take the same arguments and flags.
These arguments should be defined in ``lib/spack/spack/cmd/common/arguments.py`` so that they do not need to be redefined in multiple commands.

``<name>()``
^^^^^^^^^^^^

In order to run your command, Spack searches for a function with the same name as your command in ``<name>.py``.
This is the main method for your command and can call other helper methods to handle common tasks.

Remember, before adding a new command, think to yourself whether or not this new command is actually necessary.
Sometimes, the functionality you desire can be added to an existing command.
Also, remember to add unit tests for your command.
If it is not used very frequently, changes to the rest of Spack can cause your command to break without sufficient unit tests to prevent this from happening.

Whenever you add/remove/rename a command or flags for an existing command, make sure to update Spack's `Bash tab completion script <https://github.com/spack/spack/blob/develop/share/spack/spack-completion.bash>`_.


Writing Hooks
-------------

A hook is a callback that makes it easy to design functions that run for different events.
We do this by defining hook types and then inserting them at different places in the Spack codebase.
Whenever a hook type triggers by way of a function call, we find all the hooks of that type and run them.

Spack defines hooks by way of a module in the ``lib/spack/spack/hooks`` directory.
This module has to be registered in ``lib/spack/spack/hooks/__init__.py`` so that Spack is aware of it.
This section will cover the basic kind of hooks and how to write them.

Types of Hooks
^^^^^^^^^^^^^^

The following hooks are currently implemented to make it easy for you, the developer, to add hooks at different stages of a Spack install or similar.
If there is a hook that you would like and it is missing, you can propose to add a new one.

``pre_install(spec)``
"""""""""""""""""""""

A ``pre_install`` hook is run within the install subprocess, directly before the installation starts.
It expects a single argument of a spec.


``post_install(spec, explicit=None)``
"""""""""""""""""""""""""""""""""""""

A ``post_install`` hook is run within the install subprocess, directly after the installation finishes, but before the build stage is removed and the spec is registered in the database.
It expects two arguments: the spec and an optional boolean indicating whether this spec is being installed explicitly.

``pre_uninstall(spec)`` and ``post_uninstall(spec)``
""""""""""""""""""""""""""""""""""""""""""""""""""""

These hooks are currently used for cleaning up module files after uninstall.


Adding a New Hook Type
^^^^^^^^^^^^^^^^^^^^^^

Adding a new hook type is very simple!
In ``lib/spack/spack/hooks/__init__.py``, you can simply create a new ``HookRunner`` that is named to match your new hook.
For example, let's say you want to add a new hook called ``post_log_write`` to trigger after anything is written to a logger.
You would add it as follows:

.. code-block:: python

    # pre/post install and run by the install subprocess
    pre_install = HookRunner("pre_install")
    post_install = HookRunner("post_install")

    # hooks related to logging
    post_log_write = HookRunner("post_log_write")  # <- here is my new hook!


You then need to decide what arguments your hook would expect.
Since this is related to logging, let's say that you want a message and level.
That means that when you add a Python file to the ``lib/spack/spack/hooks`` folder with one or more callbacks intended to be triggered by this hook, you might use your new hook as follows:

.. code-block:: python

    def post_log_write(message, level):
        """Do something custom with the message and level every time we write
        to the log
        """
        print("running post_log_write!")


To use the hook, we would call it as follows somewhere in the logic to do logging.
In this example, we use it outside of a logger that is already defined:

.. code-block:: python

    import spack.hooks

    # We do something here to generate a logger and message
    spack.hooks.post_log_write(message, logger.level)


This is not to say that this would be the best way to implement an integration with the logger (you would probably want to write a custom logger, or you could have the hook defined within the logger), but it serves as an example of writing a hook.

Unit tests
----------

Unit testing
------------

Developer environment
---------------------

.. warning::

    This is an experimental feature.
    It is expected to change and you should not use it in a production environment.


When installing a package, we currently have support to export environment variables to specify adding debug flags to the build.
By default, a package installation will build without any debug flags.
However, if you want to add them, you can export:

.. code-block:: console

   export SPACK_ADD_DEBUG_FLAGS=true
   spack install zlib


If you want to add custom flags, you should export an additional variable:

.. code-block:: console

   export SPACK_ADD_DEBUG_FLAGS=true
   export SPACK_DEBUG_FLAGS="-g"
   spack install zlib

These environment variables will eventually be integrated into Spack so they are set from the command line.

Developer commands
------------------

.. _cmd-spack-doc:

``spack doc``
^^^^^^^^^^^^^

.. _cmd-spack-style:

``spack style``
^^^^^^^^^^^^^^^

``spack style`` exists to help the developer check imports and style with mypy, Flake8, isort, and (soon) Black.
To run all style checks, simply do:

.. code-block:: console

    $ spack style

To run automatic fixes for isort, you can do:

.. code-block:: console

    $ spack style --fix

You do not need any of these Python packages installed on your system for the checks to work!
Spack will bootstrap install them from packages for your use.

``spack unit-test``
^^^^^^^^^^^^^^^^^^^

See the :ref:`contributor guide section <cmd-spack-unit-test>` on ``spack unit-test``.

.. _cmd-spack-python:

``spack python``
^^^^^^^^^^^^^^^^

``spack python`` is a command that lets you import and debug things as if you were in a Spack interactive shell.
Without any arguments, it is similar to a normal interactive Python shell, except you can import ``spack`` and any other Spack modules:

.. code-block:: console

   $ spack python
   >>> from spack.version import Version
   >>> a = Version("1.2.3")
   >>> b = Version("1_2_3")
   >>> a == b
   True
   >>> c = Version("1.2.3b")
   >>> c > a
   True
   >>>

If you prefer using an IPython interpreter, given that IPython is installed, you can specify the interpreter with ``-i``:

.. code-block:: console

   $ spack python -i ipython
   In [1]:


With either interpreter you can run a single command:

.. code-block:: console

   $ spack python -c 'from spack.concretize import concretize_one; concretize_one("python")'
   ...

   $ spack python -i ipython -c 'from spack.concretize import concretize_one; concretize_one("python")'
   Out[1]: ...

or a file:

.. code-block:: console

   $ spack python ~/test_fetching.py
   $ spack python -i ipython ~/test_fetching.py

just like you would with the normal Python command.


.. _cmd-spack-blame:

``spack blame``
^^^^^^^^^^^^^^^

``spack blame`` is a way to quickly see contributors to packages or files in Spack's source tree.
For built-in packages, this shows contributors to the package files in the separate ``spack/spack-packages`` repository.
You should provide a target package name or file name to the command.
Here is an example asking to see contributions for the package "python":

.. code-block:: console

    $ spack blame python
    LAST_COMMIT  LINES  %      AUTHOR            EMAIL
    2 weeks ago  3      0.3    Mickey Mouse   <cheddar@gmouse.org>
    a month ago  927    99.7   Minnie Mouse   <swiss@mouse.org>

    2 weeks ago  930    100.0


By default, you will get a table view (shown above) sorted by date of contribution, with the most recent contribution at the top.
If you want to sort instead by percentage of code contribution, then add ``-p``:

.. code-block:: console

    $ spack blame -p python


And to see the Git blame view, add ``-g`` instead:


.. code-block:: console

    $ spack blame -g python


Finally, to get a JSON export of the data, add ``--json``:

.. code-block:: console

    $ spack blame --json python


.. _cmd-spack-url:

``spack url``
^^^^^^^^^^^^^

A package containing a single URL can be used to download several different versions of the package.
If you have ever wondered how this works, all of the magic is in :mod:`spack.url`.
This module contains methods for extracting the name and version of a package from its URL.
The name is used by ``spack create`` to guess the name of the package.
By determining the version from the URL, Spack can replace it with other versions to determine where to download them from.

The regular expressions in ``parse_name_offset`` and ``parse_version_offset`` are used to extract the name and version, but they are not perfect.
In order to debug Spack's URL parsing support, the ``spack url`` command can be used.


.. _cmd-spack-url-parse:

``spack url parse``
"""""""""""""""""""

If you need to debug a single URL, you can use the following command:

.. command-output:: spack url parse http://cache.ruby-lang.org/pub/ruby/2.2/ruby-2.2.0.tar.gz

You will notice that the name and version of this URL are correctly detected, and you can even see which regular expressions it was matched to.
However, you will notice that when it substitutes the version number in, it does not replace the ``2.2`` with ``9.9`` where we would expect ``9.9.9b`` to live.
This particular package may require a ``list_url`` or ``url_for_version`` function.

This command also accepts a ``--spider`` flag.
If provided, Spack searches for other versions of the package and prints the matching URLs.


.. _cmd-spack-url-list:

``spack url list``
""""""""""""""""""

This command lists every URL in every package in Spack.
If given the ``--color`` and ``--extrapolation`` flags, it also colors the part of the string that it detected to be the name and version.
The ``--incorrect-name`` and ``--incorrect-version`` flags can be used to print URLs that were not being parsed correctly.


.. _cmd-spack-url-summary:

``spack url summary``
"""""""""""""""""""""

This command attempts to parse every URL for every package in Spack and prints a summary of how many of them are being correctly parsed.
It also prints a histogram showing which regular expressions are being matched and how frequently:

.. command-output:: spack url summary

This command is essential for anyone adding or changing the regular expressions that parse names and versions.
By running this command before and after the change, you can make sure that your regular expression fixes more packages than it breaks.

Profiling
---------

Spack has some limited built-in support for profiling, and can report statistics using standard Python timing tools.
To use this feature, supply ``--profile`` to Spack on the command line, before any subcommands.

.. _spack-p:

``spack --profile``
^^^^^^^^^^^^^^^^^^^

``spack --profile`` output looks like this:

.. command-output:: spack --profile graph hdf5
   :ellipsis: 25

The bottom of the output shows the most time-consuming functions, slowest on top.
The profiling support is from Python's built-in tool, `cProfile <https://docs.python.org/3/library/profile.html#module-cProfile>`_.

.. _releases:

Releases
--------

This section documents Spack's release process.
It is intended for project maintainers, as the tasks described here require maintainer privileges on the Spack repository.
For others, we hope this section at least provides some insight into how the Spack project works.

.. _release-branches:

Release branches
^^^^^^^^^^^^^^^^

There are currently two types of Spack releases: :ref:`minor releases <minor-releases>` (``1.1.0``, ``1.2.0``, etc.) and :ref:`patch releases <patch-releases>` (``1.1.1``, ``1.1.2``, ``1.1.3``, etc.).
Here is a diagram of how Spack release branches work:

.. code-block:: text

   o    branch: develop  (latest version, v1.2.0.dev0)
   |
   o
   | o  branch: releases/v1.1, tag: v1.1.1
   o |
   | o  tag: v1.1.0
   o |
   | o
   |/
   o
   |
   o
   | o  branch: releases/v1.0, tag: v1.0.2
   o |
   | o  tag: v1.0.1
   o |
   | o  tag: v1.0.0
   o |
   | o
   |/
   o

The ``develop`` branch has the latest contributions, and nearly all pull requests target ``develop``.
The ``develop`` branch will report that its version is that of the next **minor** release with a ``.dev0`` suffix.

Each Spack release series also has a corresponding branch, e.g., ``releases/v1.1`` has ``v1.1.x`` versions of Spack, and ``releases/v1.0`` has ``v1.0.x`` versions.
A minor release is the first tagged version on a release branch.
Patch releases are back-ported from develop onto release branches.
This is typically done by cherry-picking bugfix commits off of ``develop``.

To avoid version churn for users of a release series, patch releases **should not** make changes that would change the concretization of packages.
They should generally only contain fixes to the Spack core.
However, sometimes priorities are such that new functionality needs to be added to a patch release.

Both minor and patch releases are tagged.
As a convenience, we also tag the latest release as ``releases/latest``, so that users can easily check it out to get the latest stable version.
See :ref:`updating-latest-release` for more details.

.. admonition:: PEP 440 compliance
   :class: note

   Spack releases up to ``v0.17`` were merged back into the ``develop`` branch to ensure that release tags would appear among its ancestors.
   Since ``v0.18`` we opted to have a linear history of the ``develop`` branch, for reasons explained `here <https://github.com/spack/spack/pull/25267>`_.
   At the same time, we converted to using `PEP 440 <https://peps.python.org/pep-0440/>`_ compliant versions.

Scheduling work for releases
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

We schedule work for **minor releases** through `milestones <https://github.com/spack/spack/milestones>`_ and `GitHub Projects <https://github.com/spack/spack/projects>`_, while **patch releases** use `labels <https://github.com/spack/spack/labels>`_.

While there can be multiple milestones open at a given time, only one is usually active.
Its name corresponds to the next major/minor version, for example ``v1.1.0``.
Important issues and pull requests should be assigned to this milestone by core developers, so that they are not forgotten at the time of release.
The milestone is closed when the release is made, and a new milestone is created for the next major/minor release, if not already there.

Bug reports in GitHub issues are automatically labelled ``bug`` and ``triage``.
Spack developers assign one of the labels ``impact-low``, ``impact-medium`` or ``impact-high``.
This will make the issue appear in the `Triaged bugs <https://github.com/orgs/spack/projects/6>`_ project board.
Important issues should be assigned to the next milestone as well, so they appear at the top of the project board.

Spack's milestones are not firm commitments so we move work between releases frequently.
If we need to make a release and some tasks are not yet done, we will simply move them to the next minor release milestone, rather than delaying the release to complete them.

Backporting bug fixes
^^^^^^^^^^^^^^^^^^^^^

When a bug is fixed in the ``develop`` branch, it is often necessary to backport the fix to one (or more) of the ``releases/vX.Y`` branches.
Only the release manager is responsible for doing backports, but Spack maintainers are responsible for labelling pull requests (and issues if no bug fix is available yet) with ``vX.Y.Z`` labels.
The labels should correspond to the future patch versions that the bug fix should be backported to.

Backports are done publicly by the release manager using a pull request named ``Backports vX.Y.Z``.
This pull request is opened from the ``backports/vX.Y.Z`` branch, targets the ``releases/vX.Y`` branch and contains a (growing) list of cherry-picked commits from the ``develop`` branch.
Typically there are one or two backport pull requests open at any given time.

.. _minor-releases:

Making minor releases
^^^^^^^^^^^^^^^^^^^^^

Assuming all required work from the milestone is completed, the steps to make the minor release are:

#. `Create a new milestone <https://github.com/spack/spack/milestones>`_ for the next major/minor release.

#. `Create a new label <https://github.com/spack/spack/labels>`_ for the next patch release.

#. Move any optional tasks that are not done to the next milestone.

#. Create a branch for the release, based on ``develop``:

   .. code-block:: console

      $ git checkout -b releases/v1.1 develop

   For a version ``vX.Y.Z``, the branch's name should be ``releases/vX.Y``.
   That is, you should create a ``releases/vX.Y`` branch if you are preparing the ``X.Y.0`` release.

#. Remove the ``dev0`` development release segment from the version tuple in ``lib/spack/spack/__init__.py``.

   The version number itself should already be correct and should not be modified.

#. Update ``CHANGELOG.md`` with major highlights in bullet form.

   Use proper Markdown formatting, like `this example from v1.0.0 <https://github.com/spack/spack/commit/b187f8758227abdfc9eb349a48f8b725aa27a162>`_.

#. Push the release branch to GitHub.

#. Make sure CI passes on the release branch, including:

   * Regular unit tests
   * Build tests
   * The E4S pipeline at `gitlab.spack.io <https://gitlab.spack.io>`_

   If CI is not passing, submit pull requests to ``develop`` as normal and keep rebasing the release branch on ``develop`` until CI passes.

#. Make sure the entire documentation is up to date.
   If documentation is outdated, submit pull requests to ``develop`` as normal and keep rebasing the release branch on ``develop``.

#. Bump the minor version in the ``develop`` branch.

   Create a pull request targeting the ``develop`` branch, bumping the minor version in ``lib/spack/spack/__init__.py`` with a ``dev0`` release segment.
   For instance, when you have just released ``v1.1.0``, set the version to ``(1, 2, 0, 'dev0')`` on ``develop``.

#. Follow the steps in :ref:`publishing-releases`.

#. Follow the steps in :ref:`updating-latest-release`.

#. Follow the steps in :ref:`announcing-releases`.


.. _patch-releases:

Making patch releases
^^^^^^^^^^^^^^^^^^^^^

To make the patch release process both efficient and transparent, we use a *backports pull request* which contains cherry-picked commits from the ``develop`` branch.
The majority of the work is to cherry-pick the bug fixes, which ideally should be done as soon as they land on ``develop``; this ensures cherry-picking happens in order and makes conflicts easier to resolve since the changes are fresh in the mind of the developer.

The backports pull request is always titled ``Backports vX.Y.Z`` and is labelled ``backports``.
It is opened from a branch named ``backports/vX.Y.Z`` and targets the ``releases/vX.Y`` branch.

Whenever a pull request labelled ``vX.Y.Z`` is merged, cherry-pick the associated squashed commit on ``develop`` to the ``backports/vX.Y.Z`` branch.
For pull requests that were rebased (or not squashed), cherry-pick each associated commit individually.
Never force-push to the ``backports/vX.Y.Z`` branch.

.. warning::

   Sometimes you may **still** get merge conflicts even if you have cherry-picked all the commits in order.
   This generally means there is some other intervening pull request that the one you are trying to pick depends on.
   In these cases, you will need to make a judgment call regarding those pull requests.
   Consider the number of affected files and/or the resulting differences.

   1. If the changes are small, you might just cherry-pick it.

   2. If the changes are large, then you may decide that this fix is not worth including in a patch release, in which case you should remove the label from the pull request.
      Remember that large, manual backports are seldom the right choice for a patch release.

When all commits are cherry-picked in the ``backports/vX.Y.Z`` branch, make the patch release as follows:

#. `Create a new label <https://github.com/spack/spack/labels>`_ ``vX.Y.{Z+1}`` for the next patch release.

#. Replace the label ``vX.Y.Z`` with ``vX.Y.{Z+1}`` for all PRs and issues that are not yet done.

#. Manually push a single commit with commit message ``Set version to vX.Y.Z`` to the ``backports/vX.Y.Z`` branch, that both bumps the Spack version number and updates the changelog:

   1. Bump the version in ``lib/spack/spack/__init__.py``.
   2. Update ``CHANGELOG.md`` with a list of the changes.

   This is typically a summary of the commits you cherry-picked onto the release branch.
   See `the changelog from v1.0.2 <https://github.com/spack/spack/commit/734c5db2121b01c373eed6538e452f18887e9e44>`_.

#. Make sure CI passes on the **backports pull request**, including:

   * Regular unit tests
   * Build tests
   * The E4S pipeline at `gitlab.spack.io <https://gitlab.spack.io>`_

#. Merge the ``Backports vX.Y.Z`` PR with the **Rebase and merge** strategy.
   This is needed to keep track in the release branch of all the commits that were cherry-picked.

#. Make sure CI passes on the last commit of the **release branch**.

#. In the rare case you need to include additional commits in the patch release after the backports PR is merged, it is best to delete the last commit ``Set version to vX.Y.Z`` from the release branch with a single force-push, open a new backports PR named ``Backports vX.Y.Z (2)``, and repeat the process.
   Avoid repeated force-pushes to the release branch.

#. Follow the steps in :ref:`publishing-releases`.

#. Follow the steps in :ref:`updating-latest-release`.

#. Follow the steps in :ref:`announcing-releases`.

#. Submit a PR to update the ``CHANGELOG.md`` in the ``develop`` branch with the addition of this patch release.

.. _publishing-releases:

Publishing a release on GitHub
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

#. Create the release in GitHub.

   * Go to `github.com/spack/spack/releases <https://github.com/spack/spack/releases>`_ and click ``Draft a new release``.

   * Set ``Tag version`` to the name of the tag that will be created.

     The name should start with ``v`` and contain *all three* parts of the version (e.g., ``v1.1.0`` or ``v1.1.1``).

   * Set ``Target`` to the ``releases/vX.Y`` branch (e.g., ``releases/v1.0``).

   * Set ``Release title`` to ``vX.Y.Z`` to match the tag (e.g., ``v1.0.1``).

   * Paste the latest release Markdown from your ``CHANGELOG.md`` file as the text.

   * Save the draft so you can keep coming back to it as you prepare the release.

#. When you are ready to finalize the release, click ``Publish release``.

#. Immediately after publishing, go back to `github.com/spack/spack/releases <https://github.com/spack/spack/releases>`_ and download the auto-generated ``.tar.gz`` file for the release.
   It is the ``Source code (tar.gz)`` link.

#. Click ``Edit`` on the release you just made and attach the downloaded release tarball as a binary.
   This does two things:

   #. Makes sure that the hash of our releases does not change over time.

      GitHub sometimes annoyingly changes the way they generate tarballs that can result in the hashes changing if you rely on the auto-generated tarball links.

   #. Gets download counts on releases visible through the GitHub API.

      GitHub tracks downloads of artifacts, but *not* the source links.
      See the `releases page <https://api.github.com/repos/spack/spack/releases>`_ and search for ``download_count`` to see this.

#. Go to `readthedocs.org <https://readthedocs.org/projects/spack>`_ and activate the release tag.

   This builds the documentation and makes the released version selectable in the versions menu.


.. _updating-latest-release:

Updating `releases/latest`
^^^^^^^^^^^^^^^^^^^^^^^^^^

If the new release is the **highest** Spack release yet, you should also tag it as ``releases/latest``.
For example, suppose the highest release is currently ``v1.1.3``:

* If you are releasing ``v1.1.4`` or ``v1.2.0``, then you should tag it with ``releases/latest``, as these are higher than ``v1.1.3``.

* If you are making a new release of an **older** minor version of Spack, e.g., ``v1.0.5``, then you should not tag it as ``releases/latest`` (as there are newer major/minor versions).

To do so, first fetch the latest tag created on GitHub, since you may not have it locally:

.. code-block:: console

   $ git fetch --force git@github.com:spack/spack tag vX.Y.Z

Then tag ``vX.Y.Z`` as ``releases/latest`` and push the individual tag to GitHub.

.. code-block:: console

   $ git tag --force releases/latest vX.Y.Z
   $ git push --force git@github.com:spack/spack releases/latest

The ``--force`` argument to ``git tag`` makes Git overwrite the existing ``releases/latest`` tag with the new one.
Do **not** use the ``--tags`` flag when pushing, as this will push *all* local tags.


.. _announcing-releases:

Announcing a release
^^^^^^^^^^^^^^^^^^^^

We announce releases in all of the major Spack communication channels.
Publishing the release takes care of GitHub.
The remaining channels are X, Slack, and the mailing list.
Here are the steps:

#. Announce the release on X.

   * Compose the tweet on the ``@spackpm`` account per the ``spack-twitter`` slack channel.

   * Be sure to include a link to the release's page on GitHub.

     You can base the tweet on `this example <https://twitter.com/spackpm/status/1231761858182307840>`_.

#. Announce the release on Slack.

   * Compose a message in the ``#announcements`` Slack channel (`spackpm.slack.com <https://spackpm.slack.com>`_).

   * Preface the message with ``@channel`` to notify even those people not currently logged in.

   * Be sure to include a link to the tweet above.

   The tweet will be shown inline so that you do not have to retype your release announcement.

#. Announce the release on the Spack mailing list.

   * Compose an email to the Spack mailing list.

   * Be sure to include a link to the release's page on GitHub.

   * It is also helpful to include some information directly in the email.

   You can base your announcement on this `example email <https://groups.google.com/forum/#!topic/spack/WT4CT9i_X4s>`_.

Once you have completed the above steps, congratulations, you are done!
You have finished making the release!
