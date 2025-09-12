# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Schema for modules.yaml configuration file.

.. literalinclude:: _spack_root/lib/spack/spack/schema/modules.py
   :lines: 16-
"""
from typing import Any, Dict

import spack.schema.environment
import spack.schema.projections

#: Definitions for parts of module schema
array_of_strings = {"type": "array", "default": [], "items": {"type": "string"}}

dependency_selection = {"type": "string", "enum": ["none", "run", "direct", "all"]}

module_file_configuration = {
    "type": "object",
    "default": {},
    "additionalProperties": False,
    "properties": {
        "filter": {
            "type": "object",
            "default": {},
            "additionalProperties": False,
            "properties": {
                "exclude_env_vars": {"type": "array", "default": [], "items": {"type": "string"}}
            },
        },
        "template": {"type": "string"},
        "autoload": dependency_selection,
        "prerequisites": dependency_selection,
        "conflict": array_of_strings,
        "load": array_of_strings,
        "suffixes": {
            "type": "object",
            "additionalKeysAreSpecs": True,
            "additionalProperties": {"type": "string"},  # key
        },
        "environment": spack.schema.environment.definition,
    },
}

projections_scheme = spack.schema.projections.properties["projections"]

common_props = {
    "verbose": {"type": "boolean", "default": False},
    "hash_length": {"type": "integer", "minimum": 0, "default": 7},
    "include": array_of_strings,
    "exclude": array_of_strings,
    "exclude_implicits": {"type": "boolean", "default": False},
    "defaults": array_of_strings,
    "hide_implicits": {"type": "boolean", "default": False},
    "naming_scheme": {"type": "string"},
    "projections": projections_scheme,
    "all": module_file_configuration,
}

tcl_configuration = {
    "type": "object",
    "default": {},
    "additionalKeysAreSpecs": True,
    "properties": {**common_props},
    "additionalProperties": module_file_configuration,
}

lmod_configuration = {
    "type": "object",
    "default": {},
    "additionalKeysAreSpecs": True,
    "properties": {
        **common_props,
        "core_compilers": array_of_strings,
        "hierarchy": array_of_strings,
        "core_specs": array_of_strings,
        "filter_hierarchy_specs": {
            "type": "object",
            "additionalKeysAreSpecs": True,
            "additionalProperties": array_of_strings,
        },
    },
    "additionalProperties": module_file_configuration,
}

module_config_properties = {
    "use_view": {"anyOf": [{"type": "string"}, {"type": "boolean"}]},
    "arch_folder": {"type": "boolean"},
    "roots": {
        "type": "object",
        "properties": {"tcl": {"type": "string"}, "lmod": {"type": "string"}},
    },
    "enable": {
        "type": "array",
        "default": [],
        "items": {"type": "string", "enum": ["tcl", "lmod"]},
    },
    "lmod": lmod_configuration,
    "tcl": tcl_configuration,
    "prefix_inspections": {
        "type": "object",
        # prefix-relative path to be inspected for existence
        "additionalProperties": array_of_strings,
    },
}


# Properties for inclusion into other schemas (requires definitions)
properties: Dict[str, Any] = {
    "modules": {
        "type": "object",
        "properties": {
            "prefix_inspections": {
                "type": "object",
                # prefix-relative path to be inspected for existence
                "additionalProperties": array_of_strings,
            }
        },
        "additionalProperties": {
            "type": "object",
            "default": {},
            "additionalProperties": False,
            "properties": module_config_properties,
        },
    }
}

#: Full schema with metadata
schema = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "Spack module file configuration file schema",
    "type": "object",
    "additionalProperties": False,
    "properties": properties,
}
