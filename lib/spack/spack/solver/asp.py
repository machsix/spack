# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
import collections
import collections.abc
import enum
import errno
import hashlib
import io
import itertools
import json
import os
import pathlib
import pprint
import random
import re
import sys
import typing
import warnings
from contextlib import contextmanager
from typing import (
    IO,
    Callable,
    Dict,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
)

import spack.vendor.archspec.cpu

import spack
import spack.compilers.config
import spack.compilers.flags
import spack.config
import spack.deptypes as dt
import spack.detection
import spack.environment as ev
import spack.error
import spack.llnl.util.lang
import spack.llnl.util.tty as tty
import spack.package_base
import spack.package_prefs
import spack.patch
import spack.paths
import spack.platforms
import spack.repo
import spack.solver.splicing
import spack.spec
import spack.store
import spack.util.crypto
import spack.util.hash
import spack.util.libc
import spack.util.module_cmd as md
import spack.util.path
import spack.util.timer
import spack.variant as vt
import spack.version as vn
import spack.version.git_ref_lookup
from spack import traverse
from spack.compilers.libraries import CompilerPropertyDetector
from spack.llnl.util.filesystem import current_file_position
from spack.llnl.util.lang import elide_list
from spack.util.file_cache import FileCache

from .core import (
    AspFunction,
    AspVar,
    NodeArgument,
    SourceContext,
    ast_sym,
    ast_type,
    clingo,
    clingo_cffi,
    extract_args,
    fn,
    parse_files,
    parse_term,
    using_libc_compatibility,
)
from .input_analysis import create_counter, create_graph_analyzer
from .requirements import RequirementKind, RequirementOrigin, RequirementParser, RequirementRule
from .reuse import ReusableSpecsSelector, SpecFilter
from .runtimes import RuntimePropertyRecorder, _external_config_with_implicit_externals
from .versions import DeclaredVersion, Provenance, concretization_version_order

GitOrStandardVersion = Union[spack.version.GitVersion, spack.version.StandardVersion]

TransformFunction = Callable[[spack.spec.Spec, List[AspFunction]], List[AspFunction]]


class OutputConfiguration(NamedTuple):
    """Data class that contains configuration on what a clingo solve should output."""

    #: Print out coarse timers for different solve phases
    timers: bool
    #: Whether to output Clingo's internal solver statistics
    stats: bool
    #: Optional output stream for the generated ASP program
    out: Optional[io.IOBase]
    #: If True, stop after setup and don't solve
    setup_only: bool


#: Default output configuration for a solve
DEFAULT_OUTPUT_CONFIGURATION = OutputConfiguration(
    timers=False, stats=False, out=None, setup_only=False
)


def default_clingo_control():
    """Return a control object with the default settings used in Spack"""
    control = clingo().Control()
    control.configuration.configuration = "tweety"
    control.configuration.solver.heuristic = "Domain"
    control.configuration.solver.opt_strategy = "usc,one,1"
    return control


@contextmanager
def named_spec(
    spec: Optional[spack.spec.Spec], name: Optional[str]
) -> Iterator[Optional[spack.spec.Spec]]:
    """Context manager to temporarily set the name of a spec"""
    if spec is None or name is None:
        yield spec
        return

    old_name = spec.name
    spec.name = name
    try:
        yield spec
    finally:
        spec.name = old_name


# Below numbers are used to map names of criteria to the order
# they appear in the solution. See concretize.lp

# The space of possible priorities for optimization targets
# is partitioned in the following ranges:
#
# [0-100) Optimization criteria for software being reused
# [100-200) Fixed criteria that are higher priority than reuse, but lower than build
# [200-300) Optimization criteria for software being built
# [300-1000) High-priority fixed criteria
# [1000-inf) Error conditions
#
# Each optimization target is a minimization with optimal value 0.

#: High fixed priority offset for criteria that supersede all build criteria
high_fixed_priority_offset = 300

#: Priority offset for "build" criteria (regular criterio shifted to
#: higher priority for specs we have to build)
build_priority_offset = 200

#: Priority offset of "fixed" criteria (those w/o build criteria)
fixed_priority_offset = 100


class OptimizationKind:
    """Enum for the optimization KIND of a criteria.

    It's not using enum.Enum since it must be serializable.
    """

    BUILD = 0
    CONCRETE = 1
    OTHER = 2


class OptimizationCriteria(NamedTuple):
    """A named tuple describing an optimization criteria."""

    priority: int
    value: int
    name: str
    kind: OptimizationKind


def build_criteria_names(costs, arg_tuples):
    """Construct an ordered mapping from criteria names to costs."""
    # pull optimization criteria names out of the solution
    priorities_names = []

    for args in arg_tuples:
        priority, name = args[:2]
        priority = int(priority)

        # Add the priority of this opt criterion and its name
        if priority < fixed_priority_offset:
            # if the priority is less than fixed_priority_offset, then it
            # has an associated build priority -- the same criterion but for
            # nodes that we have to build.
            priorities_names.append((priority, name, OptimizationKind.CONCRETE))
            build_priority = priority + build_priority_offset
            priorities_names.append((build_priority, name, OptimizationKind.BUILD))
        else:
            priorities_names.append((priority, name, OptimizationKind.OTHER))

    # sort the criteria by priority
    priorities_names = sorted(priorities_names, reverse=True)

    # We only have opt-criterion values for non-error types
    # error type criteria are excluded (they come first)
    error_criteria = len(costs) - len(priorities_names)
    costs = costs[error_criteria:]

    return [
        OptimizationCriteria(priority, value, name, status)
        for (priority, name, status), value in zip(priorities_names, costs)
    ]


def specify(spec):
    if isinstance(spec, spack.spec.Spec):
        return spec
    return spack.spec.Spec(spec)


def remove_facts(
    *to_be_removed: str,
) -> Callable[[spack.spec.Spec, List[AspFunction]], List[AspFunction]]:
    """Returns a transformation function that removes facts from the input list of facts."""

    def _remove(spec: spack.spec.Spec, facts: List[AspFunction]) -> List[AspFunction]:
        return list(filter(lambda x: x.args[0] not in to_be_removed, facts))

    return _remove


def all_libcs() -> Set[spack.spec.Spec]:
    """Return a set of all libc specs targeted by any configured compiler. If none, fall back to
    libc determined from the current Python process if dynamically linked."""
    libcs = set()
    for c in spack.compilers.config.all_compilers_from(spack.config.CONFIG):
        candidate = CompilerPropertyDetector(c).default_libc()
        if candidate is not None:
            libcs.add(candidate)

    if libcs:
        return libcs

    libc = spack.util.libc.libc_from_current_python_process()
    return {libc} if libc else set()


def libc_is_compatible(lhs: spack.spec.Spec, rhs: spack.spec.Spec) -> bool:
    return (
        lhs.name == rhs.name
        and lhs.external_path == rhs.external_path
        and lhs.version >= rhs.version
    )


def c_compiler_runs(compiler) -> bool:
    return CompilerPropertyDetector(compiler).compiler_verbose_output() is not None


def extend_flag_list(flag_list, new_flags):
    """Extend a list of flags, preserving order and precedence.

    Add new_flags at the end of flag_list.  If any flags in new_flags are
    already in flag_list, they are moved to the end so that they take
    higher precedence on the compile line.

    """
    for flag in new_flags:
        if flag in flag_list:
            flag_list.remove(flag)
        flag_list.append(flag)


def _reorder_flags(flag_list: List[spack.spec.CompilerFlag]) -> List[spack.spec.CompilerFlag]:
    """Reorder a list of flags to ensure that the order matches that of the flag group."""
    if not flag_list:
        return []

    if len({x.flag_group for x in flag_list}) != 1 or len({x.source for x in flag_list}) != 1:
        raise InternalConcretizerError(
            "internal solver error: cannot reorder compiler flags for concretized specs. "
            "Please report a bug at https://github.com/spack/spack/issues"
        )

    flag_group = flag_list[0].flag_group
    flag_source = flag_list[0].source
    flag_propagate = flag_list[0].propagate
    # Once we have the flag_group, no need to iterate over the flag_list because the
    # group represents all of them
    return [
        spack.spec.CompilerFlag(
            flag, propagate=flag_propagate, flag_group=flag_group, source=flag_source
        )
        for flag, propagate in spack.compilers.flags.tokenize_flags(
            flag_group, propagate=flag_propagate
        )
    ]


def check_packages_exist(specs):
    """Ensure all packages mentioned in specs exist."""
    repo = spack.repo.PATH
    for spec in specs:
        for s in spec.traverse():
            try:
                check_passed = repo.repo_for_pkg(s).exists(s.name) or repo.is_virtual(s.name)
            except Exception as e:
                msg = "Cannot find package: {0}".format(str(e))
                check_passed = False
                tty.debug(msg)

            if not check_passed:
                raise spack.repo.UnknownPackageError(str(s.fullname))


class Result:
    """Result of an ASP solve."""

    def __init__(self, specs, asp=None):
        self.asp = asp
        self.satisfiable = None
        self.optimal = None
        self.warnings = None
        self.nmodels = 0

        # Saved control object for reruns when necessary
        self.control = None

        # specs ordered by optimization level
        self.answers = []
        self.cores = []

        # names of optimization criteria
        self.criteria = []

        # Abstract user requests
        self.abstract_specs = specs

        # Concrete specs
        self._concrete_specs_by_input = None
        self._concrete_specs = None
        self._unsolved_specs = None

    def format_core(self, core):
        """
        Format an unsatisfiable core for human readability

        Returns a list of strings, where each string is the human readable
        representation of a single fact in the core, including a newline.

        Modeled after traceback.format_stack.
        """
        error_msg = (
            "Internal Error: ASP Result.control not populated. Please report to the spack"
            " maintainers"
        )
        assert self.control, error_msg

        symbols = dict((a.literal, a.symbol) for a in self.control.symbolic_atoms)

        core_symbols = []
        for atom in core:
            sym = symbols[atom]
            core_symbols.append(sym)

        return sorted(str(symbol) for symbol in core_symbols)

    def minimize_core(self, core):
        """
        Return a subset-minimal subset of the core.

        Clingo cores may be thousands of lines when two facts are sufficient to
        ensure unsatisfiability. This algorithm reduces the core to only those
        essential facts.
        """
        error_msg = (
            "Internal Error: ASP Result.control not populated. Please report to the spack"
            " maintainers"
        )
        assert self.control, error_msg

        min_core = core[:]
        for fact in core:
            # Try solving without this fact
            min_core.remove(fact)
            ret = self.control.solve(assumptions=min_core)
            if not ret.unsatisfiable:
                min_core.append(fact)
        return min_core

    def minimal_cores(self):
        """
        Return a list of subset-minimal unsatisfiable cores.
        """
        return [self.minimize_core(core) for core in self.cores]

    def format_minimal_cores(self):
        """List of facts for each core

        Separate cores are separated by an empty line
        """
        string_list = []
        for core in self.minimal_cores():
            if string_list:
                string_list.append("\n")
            string_list.extend(self.format_core(core))
        return string_list

    def format_cores(self):
        """List of facts for each core

        Separate cores are separated by an empty line
        Cores are not minimized
        """
        string_list = []
        for core in self.cores:
            if string_list:
                string_list.append("\n")
            string_list.extend(self.format_core(core))
        return string_list

    def raise_if_unsat(self):
        """
        Raise an appropriate error if the result is unsatisfiable.

        The error is an SolverError, and includes the minimized cores
        resulting from the solve, formatted to be human readable.
        """
        if self.satisfiable:
            return

        constraints = self.abstract_specs
        if len(constraints) == 1:
            constraints = constraints[0]

        conflicts = self.format_minimal_cores()
        raise SolverError(constraints, conflicts=conflicts)

    @property
    def specs(self):
        """List of concretized specs satisfying the initial
        abstract request.
        """
        if self._concrete_specs is None:
            self._compute_specs_from_answer_set()
        return self._concrete_specs

    @property
    def unsolved_specs(self):
        """List of tuples pairing abstract input specs that were not
        solved with their associated candidate spec from the solver
        (if the solve completed).
        """
        if self._unsolved_specs is None:
            self._compute_specs_from_answer_set()
        return self._unsolved_specs

    @property
    def specs_by_input(self):
        if self._concrete_specs_by_input is None:
            self._compute_specs_from_answer_set()
        return self._concrete_specs_by_input

    def _compute_specs_from_answer_set(self):
        if not self.satisfiable:
            self._concrete_specs = []
            self._unsolved_specs = list((x, None) for x in self.abstract_specs)
            self._concrete_specs_by_input = {}
            return

        self._concrete_specs, self._unsolved_specs = [], []
        self._concrete_specs_by_input = {}
        best = min(self.answers)
        opt, _, answer = best
        for input_spec in self.abstract_specs:
            # The specs must be unified to get here, so it is safe to associate any satisfying spec
            # with the input. Multiple inputs may be matched to the same concrete spec
            node = SpecBuilder.make_node(pkg=input_spec.name)
            if spack.repo.PATH.is_virtual(input_spec.name):
                providers = [
                    spec.name for spec in answer.values() if spec.package.provides(input_spec.name)
                ]
                node = SpecBuilder.make_node(pkg=providers[0])
            candidate = answer.get(node)

            if candidate and candidate.satisfies(input_spec):
                self._concrete_specs.append(answer[node])
                self._concrete_specs_by_input[input_spec] = answer[node]
            elif candidate and candidate.build_spec.satisfies(input_spec):
                tty.warn(
                    "explicit splice configuration has caused the concretized spec"
                    f" {candidate} not to satisfy the input spec {input_spec}"
                )
                self._concrete_specs.append(answer[node])
                self._concrete_specs_by_input[input_spec] = answer[node]
            else:
                self._unsolved_specs.append((input_spec, candidate))

    @staticmethod
    def format_unsolved(unsolved_specs):
        """Create a message providing info on unsolved user specs and for
        each one show the associated candidate spec from the solver (if
        there is one).
        """
        msg = "Unsatisfied input specs:"
        for input_spec, candidate in unsolved_specs:
            msg += f"\n\tInput spec: {str(input_spec)}"
            if candidate:
                msg += f"\n\tCandidate spec: {candidate.long_spec}"
            else:
                msg += "\n\t(No candidate specs from solver)"
        return msg

    def to_dict(self, test: bool = False) -> dict:
        """Produces dict representation of Result object

        Does not include anything related to unsatisfiability as we
        are only interested in storing satisfiable results
        """
        serial_node_arg = (
            lambda node_dict: f"""{{"id": "{node_dict.id}", "pkg": "{node_dict.pkg}"}}"""
        )
        ret = dict()
        ret["asp"] = self.asp
        ret["criteria"] = self.criteria
        ret["optimal"] = self.optimal
        ret["warnings"] = self.warnings
        ret["nmodels"] = self.nmodels
        ret["abstract_specs"] = [str(x) for x in self.abstract_specs]
        ret["satisfiable"] = self.satisfiable
        serial_answers = []
        for answer in self.answers:
            serial_answer = answer[:2]
            serial_answer_dict = {}
            for node, spec in answer[2].items():
                serial_answer_dict[serial_node_arg(node)] = spec.to_dict()
            serial_answer = serial_answer + (serial_answer_dict,)
            serial_answers.append(serial_answer)
        ret["answers"] = serial_answers
        ret["specs_by_input"] = {}
        input_specs = {} if not self.specs_by_input else self.specs_by_input
        for input, spec in input_specs.items():
            ret["specs_by_input"][str(input)] = spec.to_dict()
        return ret

    @staticmethod
    def from_dict(obj: dict):
        """Returns Result object from compatible dictionary"""

        def _dict_to_node_argument(dict):
            id = dict["id"]
            pkg = dict["pkg"]
            return NodeArgument(id=id, pkg=pkg)

        def _str_to_spec(spec_str):
            return spack.spec.Spec(spec_str)

        def _dict_to_spec(spec_dict):
            loaded_spec = spack.spec.Spec.from_dict(spec_dict)
            _ensure_external_path_if_external(loaded_spec)
            spack.spec.Spec.ensure_no_deprecated(loaded_spec)
            return loaded_spec

        asp = obj.get("asp")
        spec_list = obj.get("abstract_specs")
        if not spec_list:
            raise RuntimeError("Invalid json for concretization Result object")
        if spec_list:
            spec_list = [_str_to_spec(x) for x in spec_list]
        result = Result(spec_list, asp)
        result.criteria = obj.get("criteria")
        result.optimal = obj.get("optimal")
        result.warnings = obj.get("warnings")
        result.nmodels = obj.get("nmodels")
        result.satisfiable = obj.get("satisfiable")
        result._unsolved_specs = []
        answers = []
        for answer in obj.get("answers", []):
            loaded_answer = answer[:2]
            answer_node_dict = {}
            for node, spec in answer[2].items():
                answer_node_dict[_dict_to_node_argument(json.loads(node))] = _dict_to_spec(spec)
            loaded_answer.append(answer_node_dict)
            answers.append(tuple(loaded_answer))
        result.answers = answers
        result._concrete_specs_by_input = {}
        result._concrete_specs = []
        for input, spec in obj.get("specs_by_input", {}).items():
            result._concrete_specs_by_input[_str_to_spec(input)] = _dict_to_spec(spec)
            result._concrete_specs.append(_dict_to_spec(spec))
        return result


class ConcretizationCache:
    """Store for Spack concretization results and statistics

    Serializes solver result objects and statistics to json and stores
    at a given endpoint in a cache associated by the sha256 of the
    asp problem and the involved control files.
    """

    def __init__(self, root: Union[str, None] = None):
        root = root or spack.config.get(
            "config:concretization_cache:url", spack.paths.default_conc_cache_path
        )
        self.root = pathlib.Path(spack.util.path.canonicalize_path(root))
        self._fc = FileCache(self.root)
        self._cache_manifest = ".cache_manifest"
        self._manifest_queue: List[Tuple[pathlib.Path, int]] = []

    def cleanup(self):
        """Prunes the concretization cache according to configured size and entry
        count limits. Cleanup is done in FIFO ordering."""
        # TODO: determine a better default
        entry_limit = spack.config.get("config:concretization_cache:entry_limit", 1000)
        bytes_limit = spack.config.get("config:concretization_cache:size_limit", 3e8)
        # lock the entire buildcache as we're removing a lot of data from the
        # manifest and cache itself
        with self._fc.read_transaction(self._cache_manifest) as f:
            count, cache_bytes = self._extract_cache_metadata(f)
            if not count or not cache_bytes:
                return
            entry_count = int(count)
            manifest_bytes = int(cache_bytes)
            # move beyond the metadata entry
            f.readline()
            if entry_count > entry_limit and entry_limit > 0:
                with self._fc.write_transaction(self._cache_manifest) as (old, new):
                    # prune the oldest 10% or until we have removed 10% of
                    # total bytes starting from oldest entry
                    # TODO: make this configurable?
                    prune_count = entry_limit // 10
                    lines_to_prune = f.readlines(prune_count)
                    for i, line in enumerate(lines_to_prune):
                        sha, cache_entry_bytes = self._parse_manifest_entry(line)
                        if sha and cache_entry_bytes:
                            cache_path = self._cache_path_from_hash(sha)
                            if self._fc.remove(cache_path):
                                entry_count -= 1
                                manifest_bytes -= int(cache_entry_bytes)
                        else:
                            tty.warn(
                                f"Invalid concretization cache entry: '{line}' on line: {i+1}"
                            )
                    self._write_manifest(f, entry_count, manifest_bytes)

            elif manifest_bytes > bytes_limit and bytes_limit > 0:
                with self._fc.write_transaction(self._cache_manifest) as (old, new):
                    # take 10% of current size off
                    prune_amount = bytes_limit // 10
                    total_pruned = 0
                    i = 0
                    while total_pruned < prune_amount:
                        sha, manifest_cache_bytes = self._parse_manifest_entry(f.readline())
                        if sha and manifest_cache_bytes:
                            entry_bytes = int(manifest_cache_bytes)
                            cache_path = self.root / sha[:2] / sha
                            if self._safe_remove(cache_path):
                                entry_count -= 1
                                entry_bytes -= entry_bytes
                                total_pruned += entry_bytes
                        else:
                            tty.warn(
                                "Invalid concretization cache entry "
                                f"'{sha} {manifest_cache_bytes}' on line: {i}"
                            )
                        i += 1
                    self._write_manifest(f, entry_count, manifest_bytes)
            for cache_dir in self.root.iterdir():
                if cache_dir.is_dir() and not any(cache_dir.iterdir()):
                    self._safe_remove(cache_dir)

    def cache_entries(self):
        """Generator producing cache entries"""
        for cache_dir in self.root.iterdir():
            # ensure component is cache entry directory
            # not metadata file
            if cache_dir.is_dir():
                for cache_entry in cache_dir.iterdir():
                    if not cache_entry.is_dir():
                        yield cache_entry
                    else:
                        raise RuntimeError(
                            "Improperly formed concretization cache. "
                            f"Directory {cache_entry.name} is improperly located "
                            "within the concretization cache."
                        )

    def _parse_manifest_entry(self, line):
        """Returns parsed manifest entry lines
        with handling for invalid reads."""
        if line:
            cache_values = line.strip("\n").split(" ")
            if len(cache_values) < 2:
                tty.warn(f"Invalid cache entry at {line}")
                return None, None
        return None, None

    def _write_manifest(self, manifest_file, entry_count, entry_bytes):
        """Writes new concretization cache manifest file.

        Arguments:
            manifest_file: IO stream opened for readin
                            and writing wrapping the manifest file
                            with cursor at calltime set to location
                            where manifest should be truncated
            entry_count: new total entry count
            entry_bytes: new total entry bytes count

        """
        persisted_entries = manifest_file.readlines()
        manifest_file.truncate(0)
        manifest_file.write(f"{entry_count} {entry_bytes}\n")
        manifest_file.writelines(persisted_entries)

    def _results_from_cache(self, cache_entry_buffer: IO[str]) -> Union[Result, None]:
        """Returns a Results object from the concretizer cache

        Reads the cache hit and uses `Result`'s own deserializer
        to produce a new Result object
        """

        with current_file_position(cache_entry_buffer, 0):
            cache_str = cache_entry_buffer.read()
            # TODO: Should this be an error if None?
            # Same for _stats_from_cache
            if cache_str:
                cache_entry = json.loads(cache_str)
                result_json = cache_entry["results"]
                return Result.from_dict(result_json)
        return None

    def _stats_from_cache(self, cache_entry_buffer: IO[str]) -> Union[List, None]:
        """Returns concretization statistic from the
        concretization associated with the cache.

        Deserialzes the the json representation of the
        statistics covering the cached concretization run
        and returns the Python data structures
        """
        with current_file_position(cache_entry_buffer, 0):
            cache_str = cache_entry_buffer.read()
            if cache_str:
                return json.loads(cache_str)["statistics"]
        return None

    def _extract_cache_metadata(self, cache_stream: IO[str]):
        """Extracts and returns cache entry count and bytes count from head of manifest
        file"""
        # make sure we're always reading from the beginning of the stream
        # concretization cache manifest data lives at the top of the file
        with current_file_position(cache_stream, 0):
            return self._parse_manifest_entry(cache_stream.readline())

    def _prefix_digest(self, problem: str) -> Tuple[str, str]:
        """Return the first two characters of, and the full, sha256 of the given asp problem"""
        prob_digest = hashlib.sha256(problem.encode()).hexdigest()
        prefix = prob_digest[:2]
        return prefix, prob_digest

    def _cache_path_from_problem(self, problem: str) -> pathlib.Path:
        """Returns a Path object representing the path to the cache
        entry for the given problem"""
        prefix, digest = self._prefix_digest(problem)
        return pathlib.Path(prefix) / digest

    def _cache_path_from_hash(self, hash: str) -> pathlib.Path:
        """Returns a Path object representing the cache entry
        corresponding to the given sha256 hash"""
        return pathlib.Path(hash[:2]) / hash

    def _lock_prefix_from_cache_path(self, cache_path: str):
        """Returns the bit location corresponding to a given cache entry path
        for file locking"""
        return spack.util.hash.base32_prefix_bits(
            spack.util.hash.b32_hash(cache_path), spack.util.crypto.bit_length(sys.maxsize)
        )

    def flush_manifest(self):
        """Updates the concretization cache manifest file after a cache write operation
        Updates the current byte count and entry counts and writes to the head of the
        manifest file"""
        manifest_file = self.root / self._cache_manifest
        manifest_file.touch(exist_ok=True)
        with open(manifest_file, "r+", encoding="utf-8") as f:
            # check if manifest is empty
            count, cache_bytes = self._extract_cache_metadata(f)
            if not count or not cache_bytes:
                # cache is unintialized
                count = 0
                cache_bytes = 0
            f.seek(0, io.SEEK_END)
            for manifest_update in self._manifest_queue:
                entry_path, entry_bytes = manifest_update
                count += 1
                cache_bytes += entry_bytes
                f.write(f"{entry_path.name} {entry_bytes}")
            f.seek(0, io.SEEK_SET)
            new_stats = f"{int(count)+1} {int(cache_bytes)}\n"
            f.write(new_stats)

    def _register_cache_update(self, cache_path: pathlib.Path, bytes_written: int):
        """Adds manifest entry to update queue for later updates to the manifest"""
        self._manifest_queue.append((cache_path, bytes_written))

    def _safe_remove(self, cache_dir: pathlib.Path):
        """Removes cache entries with handling for the case where the entry has been
        removed already or there are multiple cache entries in a directory"""
        try:
            if cache_dir.is_dir():
                cache_dir.rmdir()
            else:
                cache_dir.unlink()
            return True
        except FileNotFoundError:
            # This is acceptable, removal is idempotent
            pass
        except OSError as e:
            if e.errno == errno.ENOTEMPTY:
                # there exists another cache entry in this directory, don't clean yet
                pass
        return False

    def store(self, problem: str, result: Result, statistics: List, test: bool = False):
        """Creates entry in concretization cache for problem if none exists,
        storing the concretization Result object and statistics in the cache
        as serialized json joined as a single file.

        Hash membership is computed based on the sha256 of the provided asp
        problem.
        """
        cache_path = self._cache_path_from_problem(problem)
        if self._fc.init_entry(cache_path):
            # if an entry for this conc hash exists already, we're don't want
            # to overwrite, just exit
            tty.debug(f"Cache entry {cache_path} exists, will not be overwritten")
            return
        with self._fc.write_transaction(cache_path) as (old, new):
            if old:
                # Entry for this conc hash exists already, do not overwrite
                tty.debug(f"Cache entry {cache_path} exists, will not be overwritten")
                return
            cache_dict = {"results": result.to_dict(test=test), "statistics": statistics}
            bytes_written = new.write(json.dumps(cache_dict))
            self._register_cache_update(cache_path, bytes_written)

    def fetch(self, problem: str) -> Union[Tuple[Result, List], Tuple[None, None]]:
        """Returns the concretization cache result for a lookup based on the given problem.

        Checks the concretization cache for the given problem, and either returns the
        Python objects cached on disk representing the concretization results and statistics
        or returns none if no cache entry was found.
        """
        cache_path = self._cache_path_from_problem(problem)
        result, statistics = None, None
        with self._fc.read_transaction(cache_path) as f:
            if f:
                result = self._results_from_cache(f)
                statistics = self._stats_from_cache(f)
        if result and statistics:
            tty.debug(f"Concretization cache hit at {str(cache_path)}")
            return result, statistics
        tty.debug(f"Concretization cache miss at {str(cache_path)}")
        return None, None


CONC_CACHE: ConcretizationCache = spack.llnl.util.lang.Singleton(
    lambda: ConcretizationCache()
)  # type: ignore


def _is_checksummed_git_version(v):
    return isinstance(v, vn.GitVersion) and v.is_commit


def _is_checksummed_version(version_info: Tuple[GitOrStandardVersion, dict]):
    """Returns true iff the version is not a moving target"""
    version, info = version_info
    if isinstance(version, spack.version.StandardVersion):
        if any(h in info for h in spack.util.crypto.hashes.keys()) or "checksum" in info:
            return True
        return "commit" in info and len(info["commit"]) == 40
    return _is_checksummed_git_version(version)


def _spec_with_default_name(spec_str, name):
    """Return a spec with a default name if none is provided, used for requirement specs"""
    spec = spack.spec.Spec(spec_str)
    if not spec.name:
        spec.name = name
    return spec


class ErrorHandler:
    def __init__(self, model, input_specs: List[spack.spec.Spec]):
        self.model = model
        self.input_specs = input_specs
        self.full_model = None

    def multiple_values_error(self, attribute, pkg):
        return f'Cannot select a single "{attribute}" for package "{pkg}"'

    def no_value_error(self, attribute, pkg):
        return f'Cannot select a single "{attribute}" for package "{pkg}"'

    def _get_cause_tree(
        self,
        cause: Tuple[str, str],
        conditions: Dict[str, str],
        condition_causes: List[Tuple[Tuple[str, str], Tuple[str, str]]],
        seen: Set,
        indent: str = "        ",
    ) -> List[str]:
        """
        Implementation of recursion for self.get_cause_tree. Much of this operates on tuples
        (condition_id, set_id) in which the latter idea means that the condition represented by
        the former held in the condition set represented by the latter.
        """
        seen.add(cause)
        parents = [c for e, c in condition_causes if e == cause and c not in seen]
        local = f"required because {conditions[cause[0]]} "

        return [indent + local] + [
            c
            for parent in parents
            for c in self._get_cause_tree(
                parent, conditions, condition_causes, seen, indent=indent + "  "
            )
        ]

    def get_cause_tree(self, cause: Tuple[str, str]) -> List[str]:
        """
        Get the cause tree associated with the given cause.

        Arguments:
            cause: The root cause of the tree (final condition)

        Returns:
            A list of strings describing the causes, formatted to display tree structure.
        """
        conditions: Dict[str, str] = dict(extract_args(self.full_model, "condition_reason"))
        condition_causes: List[Tuple[Tuple[str, str], Tuple[str, str]]] = list(
            ((Effect, EID), (Cause, CID))
            for Effect, EID, Cause, CID in extract_args(self.full_model, "condition_cause")
        )
        return self._get_cause_tree(cause, conditions, condition_causes, set())

    def handle_error(self, msg, *args):
        """Handle an error state derived by the solver."""
        if msg == "multiple_values_error":
            return self.multiple_values_error(*args)

        if msg == "no_value_error":
            return self.no_value_error(*args)

        try:
            idx = args.index("startcauses")
        except ValueError:
            msg_args = args
            causes = []
        else:
            msg_args = args[:idx]
            cause_args = args[idx + 1 :]
            cause_args_conditions = cause_args[::2]
            cause_args_ids = cause_args[1::2]
            causes = list(zip(cause_args_conditions, cause_args_ids))

        msg = msg.format(*msg_args)

        # For variant formatting, we sometimes have to construct specs
        # to format values properly. Find/replace all occurances of
        # Spec(...) with the string representation of the spec mentioned
        specs_to_construct = re.findall(r"Spec\(([^)]*)\)", msg)
        for spec_str in specs_to_construct:
            msg = msg.replace(f"Spec({spec_str})", str(spack.spec.Spec(spec_str)))

        for cause in set(causes):
            for c in self.get_cause_tree(cause):
                msg += f"\n{c}"

        return msg

    def message(self, errors) -> str:
        input_specs = ", ".join(elide_list([f"`{s}`" for s in self.input_specs], 5))
        header = f"failed to concretize {input_specs} for the following reasons:"
        messages = (
            f"    {idx+1:2}. {self.handle_error(msg, *args)}"
            for idx, (_, msg, args) in enumerate(errors)
        )
        return "\n".join((header, *messages))

    def raise_if_errors(self):
        initial_error_args = extract_args(self.model, "error")
        if not initial_error_args:
            return

        error_causation = clingo().Control()

        parent_dir = pathlib.Path(__file__).parent
        errors_lp = parent_dir / "error_messages.lp"

        def on_model(model):
            self.full_model = model.symbols(shown=True, terms=True)

        with error_causation.backend() as backend:
            for atom in self.model:
                atom_id = backend.add_atom(atom)
                backend.add_rule([atom_id], [], choice=False)

            error_causation.load(str(errors_lp))
            error_causation.ground([("base", []), ("error_messages", [])])
            _ = error_causation.solve(on_model=on_model)

        # No choices so there will be only one model
        error_args = extract_args(self.full_model, "error")
        errors = sorted(
            [(int(priority), msg, args) for priority, msg, *args in error_args], reverse=True
        )
        try:
            msg = self.message(errors)
        except Exception as e:
            msg = (
                f"unexpected error during concretization [{str(e)}]. "
                f"Please report a bug at https://github.com/spack/spack/issues"
            )
            raise spack.error.SpackError(msg) from e
        raise UnsatisfiableSpecError(msg)


class PyclingoDriver:
    def __init__(self, cores=True):
        """Driver for the Python clingo interface.

        Arguments:
            cores (bool): whether to generate unsatisfiable cores for better
                error reporting.
        """
        self.cores = cores
        # This attribute will be reset at each call to solve
        self.control = None

    def solve(self, setup, specs, reuse=None, output=None, control=None, allow_deprecated=False):
        """Set up the input and solve for dependencies of ``specs``.

        Arguments:
            setup (SpackSolverSetup): An object to set up the ASP problem.
            specs (list): List of ``Spec`` objects to solve for.
            reuse (None or list): list of concrete specs that can be reused
            output (None or OutputConfiguration): configuration object to set
                the output of this solve.
            control (clingo.Control): configuration for the solver. If None,
                default values will be used
            allow_deprecated: if True, allow deprecated versions in the solve

        Return:
            A tuple of the solve result, the timer for the different phases of the
            solve, and the internal statistics from clingo.
        """
        # avoid circular import
        from spack.bootstrap.core import ensure_winsdk_external_or_raise

        output = output or DEFAULT_OUTPUT_CONFIGURATION
        timer = spack.util.timer.Timer()

        # Initialize the control object for the solver
        self.control = control or default_clingo_control()

        # ensure core deps are present on Windows
        # needs to modify active config scope, so cannot be run within
        # bootstrap config scope
        if sys.platform == "win32":
            tty.debug("Ensuring basic dependencies {win-sdk, wgl} available")
            ensure_winsdk_external_or_raise()
        control_files = ["concretize.lp", "heuristic.lp", "display.lp", "direct_dependency.lp"]
        if not setup.concretize_everything:
            control_files.append("when_possible.lp")
        if using_libc_compatibility():
            control_files.append("libc_compatibility.lp")
        else:
            control_files.append("os_compatibility.lp")
        if setup.enable_splicing:
            control_files.append("splices.lp")

        timer.start("setup")
        asp_problem = setup.setup(specs, reuse=reuse, allow_deprecated=allow_deprecated)
        if output.out is not None:
            output.out.write(asp_problem)
        if output.setup_only:
            return Result(specs), None, None
        timer.stop("setup")

        timer.start("cache-check")
        timer.start("ordering")
        # ensure deterministic output
        problem_repr = "\n".join(sorted(asp_problem.split("\n")))
        timer.stop("ordering")
        parent_dir = os.path.dirname(__file__)
        full_path = lambda x: os.path.join(parent_dir, x)
        abs_control_files = [full_path(x) for x in control_files]
        for ctrl_file in abs_control_files:
            with open(ctrl_file, "r", encoding="utf-8") as f:
                problem_repr += "\n" + f.read()

        result = None
        conc_cache_enabled = spack.config.get("config:concretization_cache:enable", False)
        if conc_cache_enabled:
            result, concretization_stats = CONC_CACHE.fetch(problem_repr)

        timer.stop("cache-check")
        if not result:
            timer.start("load")
            # Add the problem instance
            self.control.add("base", [], asp_problem)
            # Load the files
            [self.control.load(lp) for lp in abs_control_files]
            timer.stop("load")

            # Grounding is the first step in the solve -- it turns our facts
            # and first-order logic rules into propositional logic.
            timer.start("ground")
            self.control.ground([("base", [])])
            timer.stop("ground")

            # With a grounded program, we can run the solve.
            models = []  # stable models if things go well
            cores = []  # unsatisfiable cores if they do not

            def on_model(model):
                models.append((model.cost, model.symbols(shown=True, terms=True)))

            solve_kwargs = {
                "assumptions": setup.assumptions,
                "on_model": on_model,
                "on_core": cores.append,
            }

            if clingo_cffi():
                solve_kwargs["on_unsat"] = cores.append

            timer.start("solve")
            time_limit = spack.config.CONFIG.get("concretizer:timeout", -1)
            error_on_timeout = spack.config.CONFIG.get("concretizer:error_on_timeout", True)
            # Spack uses 0 to set no time limit, clingo API uses -1
            if time_limit == 0:
                time_limit = -1
            with self.control.solve(**solve_kwargs, async_=True) as handle:
                finished = handle.wait(time_limit)
                if not finished:
                    specs_str = ", ".join(
                        spack.llnl.util.lang.elide_list([str(s) for s in specs], 4)
                    )
                    header = (
                        f"Spack is taking more than {time_limit} seconds to solve for {specs_str}"
                    )
                    if error_on_timeout:
                        raise UnsatisfiableSpecError(f"{header}, stopping concretization")
                    warnings.warn(f"{header}, using the best configuration found so far")
                    handle.cancel()

                solve_result = handle.get()
            timer.stop("solve")

            # once done, construct the solve result
            result = Result(specs)
            result.satisfiable = solve_result.satisfiable

            if result.satisfiable:
                timer.start("construct_specs")
                # get the best model
                builder = SpecBuilder(specs, hash_lookup=setup.reusable_and_possible)
                min_cost, best_model = min(models)

                # first check for errors
                error_handler = ErrorHandler(best_model, specs)
                error_handler.raise_if_errors()

                # build specs from spec attributes in the model
                spec_attrs = [
                    (name, tuple(rest)) for name, *rest in extract_args(best_model, "attr")
                ]
                answers = builder.build_specs(spec_attrs)

                # add best spec to the results
                result.answers.append((list(min_cost), 0, answers))

                # get optimization criteria
                criteria_args = extract_args(best_model, "opt_criterion")
                result.criteria = build_criteria_names(min_cost, criteria_args)

                # record the number of models the solver considered
                result.nmodels = len(models)

                # record the possible dependencies in the solve
                result.possible_dependencies = setup.pkgs
                timer.stop("construct_specs")
                timer.stop()
            elif cores:
                result.control = self.control
                result.cores.extend(cores)

            result.raise_if_unsat()

            if result.satisfiable and result.unsolved_specs and setup.concretize_everything:
                raise OutputDoesNotSatisfyInputError(result.unsolved_specs)

            if conc_cache_enabled:
                CONC_CACHE.store(problem_repr, result, self.control.statistics, test=setup.tests)
            concretization_stats = self.control.statistics
        if output.timers:
            timer.write_tty()
            print()

        if output.stats:
            print("Statistics:")
            pprint.pprint(concretization_stats)
        return result, timer, concretization_stats


class ConcreteSpecsByHash(collections.abc.Mapping):
    """Mapping containing concrete specs keyed by DAG hash.

    The mapping is ensured to be consistent, i.e. if a spec in the mapping has a dependency with
    hash X, it is ensured to be the same object in memory as the spec keyed by X.
    """

    def __init__(self) -> None:
        self.data: Dict[str, spack.spec.Spec] = {}
        self.explicit: Set[str] = set()

    def __getitem__(self, dag_hash: str) -> spack.spec.Spec:
        return self.data[dag_hash]

    def explicit_items(self) -> Iterator[Tuple[str, spack.spec.Spec]]:
        """Iterate on items that have been added explicitly, and not just as a dependency
        of other nodes.
        """
        for h, s in self.items():
            # We need to make an exception for gcc-runtime, until we can splice it.
            if h in self.explicit or s.name == "gcc-runtime":
                yield h, s

    def add(self, spec: spack.spec.Spec) -> bool:
        """Adds a new concrete spec to the mapping. Returns True if the spec was just added,
        False if the spec was already in the mapping.

        Calling this function marks the spec as added explicitly.

        Args:
            spec: spec to be added

        Raises:
            ValueError: if the spec is not concrete
        """
        if not spec.concrete:
            msg = (
                f"trying to store the non-concrete spec '{spec}' in a container "
                f"that only accepts concrete"
            )
            raise ValueError(msg)

        dag_hash = spec.dag_hash()
        self.explicit.add(dag_hash)
        if dag_hash in self.data:
            return False

        # Here we need to iterate on the input and rewire the copy.
        self.data[spec.dag_hash()] = spec.copy(deps=False)
        nodes_to_reconstruct = [spec]

        while nodes_to_reconstruct:
            input_parent = nodes_to_reconstruct.pop()
            container_parent = self.data[input_parent.dag_hash()]

            for edge in input_parent.edges_to_dependencies():
                input_child = edge.spec
                container_child = self.data.get(input_child.dag_hash())
                # Copy children that don't exist yet
                if container_child is None:
                    container_child = input_child.copy(deps=False)
                    self.data[input_child.dag_hash()] = container_child
                    nodes_to_reconstruct.append(input_child)

                # Rewire edges
                container_parent.add_dependency_edge(
                    dependency_spec=container_child, depflag=edge.depflag, virtuals=edge.virtuals
                )
        return True

    def __len__(self) -> int:
        return len(self.data)

    def __iter__(self):
        return iter(self.data)


# types for condition caching in solver setup
ConditionSpecKey = Tuple[str, Optional[TransformFunction]]
ConditionIdFunctionPair = Tuple[int, List[AspFunction]]
ConditionSpecCache = Dict[str, Dict[ConditionSpecKey, ConditionIdFunctionPair]]


class ConstraintOrigin(enum.Enum):
    """Generates identifiers that can be pased into the solver attached
    to constraints, and then later retrieved to determine the origin of
    those constraints when ``SpecBuilder`` creates Specs from the solve
    result.
    """

    CONDITIONAL_SPEC = 0
    DEPENDS_ON = 1
    REQUIRE = 2

    @staticmethod
    def _SUFFIXES() -> Dict["ConstraintOrigin", str]:
        return {
            ConstraintOrigin.CONDITIONAL_SPEC: "_cond",
            ConstraintOrigin.DEPENDS_ON: "_dep",
            ConstraintOrigin.REQUIRE: "_req",
        }

    @staticmethod
    def append_type_suffix(pkg_id: str, kind: "ConstraintOrigin") -> str:
        """Given a package identifier and a constraint kind, generate a string ID."""
        suffix = ConstraintOrigin._SUFFIXES()[kind]
        return f"{pkg_id}{suffix}"

    @staticmethod
    def strip_type_suffix(source: str) -> Tuple[int, Optional[str]]:
        """Take a combined package/type ID generated by
        ``append_type_suffix``, and extract the package ID and
        an associated weight.
        """
        if not source:
            return -1, None
        for kind, suffix in ConstraintOrigin._SUFFIXES().items():
            if source.endswith(suffix):
                return kind.value, source[: -len(suffix)]
        return -1, source


class ConditionIdContext(SourceContext):
    """Derived from a ``ConditionContext``: for clause-sets generated by
    imposed/required specs, stores an associated transform.

    This is primarily used for tracking whether we are generating clauses
    in the context of a required spec, or for an imposed spec.

    Is not a subclass of ``ConditionContext`` because it exists in a
    lower-level context with less information.
    """

    def __init__(self):
        super().__init__()
        self.transform = None


class ConditionContext(SourceContext):
    """Tracks context in which a condition (i.e. ``SpackSolverSetup.condition``)
    is generated (e.g. for a ``depends_on``).

    This may modify the required/imposed specs generated as relevant
    for the context.
    """

    def __init__(self):
        super().__init__()
        # transformation applied to facts from the required spec. Defaults
        # to leave facts as they are.
        self.transform_required = None
        # transformation applied to facts from the imposed spec. Defaults
        # to removing "node" and "virtual_node" facts.
        self.transform_imposed = None
        # Whether to wrap direct dependency facts as node requirements,
        # imposed by the parent. If None, the default is used, which is:
        # - wrap head of rules
        # - do not wrap body of rules
        self.wrap_node_requirement: Optional[bool] = None

    def requirement_context(self) -> ConditionIdContext:
        ctxt = ConditionIdContext()
        ctxt.source = self.source
        ctxt.transform = self.transform_required
        ctxt.wrap_node_requirement = self.wrap_node_requirement
        return ctxt

    def impose_context(self) -> ConditionIdContext:
        ctxt = ConditionIdContext()
        ctxt.source = self.source
        ctxt.transform = self.transform_imposed
        ctxt.wrap_node_requirement = self.wrap_node_requirement
        return ctxt


class SpackSolverSetup:
    """Class to set up and run a Spack concretization solve."""

    gen: "ProblemInstanceBuilder"

    def __init__(self, tests: bool = False):
        self.possible_graph = create_graph_analyzer()

        # these are all initialized in setup()
        self.requirement_parser = RequirementParser(spack.config.CONFIG)
        self.possible_virtuals: Set[str] = set()

        self.assumptions: List[Tuple["clingo.Symbol", bool]] = []  # type: ignore[name-defined]
        self.declared_versions: Dict[str, List[DeclaredVersion]] = collections.defaultdict(list)
        self.possible_versions: Dict[str, Set[GitOrStandardVersion]] = collections.defaultdict(set)
        self.git_commit_versions: Dict[str, Dict[GitOrStandardVersion, str]] = (
            collections.defaultdict(dict)
        )
        self.deprecated_versions: Dict[str, Set[GitOrStandardVersion]] = collections.defaultdict(
            set
        )

        self.possible_compilers: List[spack.spec.Spec] = []
        self.rejected_compilers: Set[spack.spec.Spec] = set()
        self.possible_oses: Set = set()
        self.variant_values_from_specs: Set = set()
        self.version_constraints: Set = set()
        self.target_constraints: Set = set()
        self.default_targets: List = []
        self.compiler_version_constraints: Set = set()
        self.post_facts: List = []
        self.variant_ids_by_def_id: Dict[int, int] = {}

        self.reusable_and_possible: ConcreteSpecsByHash = ConcreteSpecsByHash()

        self._id_counter: Iterator[int] = itertools.count()
        self._trigger_cache: ConditionSpecCache = collections.defaultdict(dict)
        self._effect_cache: ConditionSpecCache = collections.defaultdict(dict)

        # Caches to optimize the setup phase of the solver
        self.target_specs_cache = None

        # whether to add installed/binary hashes to the solve
        self.tests = tests

        # If False allows for input specs that are not solved
        self.concretize_everything = True

        # Set during the call to setup
        self.pkgs: Set[str] = set()
        self.explicitly_required_namespaces: Dict[str, str] = {}

        # list of unique libc specs targeted by compilers (or an educated guess if no compiler)
        self.libcs: List[spack.spec.Spec] = []

        # If true, we have to load the code for synthesizing splices
        self.enable_splicing: bool = spack.config.CONFIG.get("concretizer:splice:automatic")

    def pkg_version_rules(self, pkg):
        """Output declared versions of a package.

        This uses self.declared_versions so that we include any versions
        that arise from a spec.
        """

        def key_fn(version):
            # Origins are sorted by "provenance" first, see the Provenance enumeration above
            return version.origin, version.idx

        if isinstance(pkg, str):
            pkg = self.pkg_class(pkg)

        declared_versions = self.declared_versions[pkg.name]
        partially_sorted_versions = sorted(set(declared_versions), key=key_fn)

        most_to_least_preferred = []
        for _, group in itertools.groupby(partially_sorted_versions, key=key_fn):
            most_to_least_preferred.extend(
                list(sorted(group, reverse=True, key=lambda x: vn.ver(x.version)))
            )

        for weight, declared_version in enumerate(most_to_least_preferred):
            self.gen.fact(
                fn.pkg_fact(
                    pkg.name,
                    fn.version_declared(
                        declared_version.version, weight, str(declared_version.origin)
                    ),
                )
            )

        for v in self.possible_versions[pkg.name]:
            if pkg.needs_commit(v):
                commit = pkg.version_or_package_attr("commit", v, "")
                self.git_commit_versions[pkg.name][v] = commit

        # Declare deprecated versions for this package, if any
        deprecated = self.deprecated_versions[pkg.name]
        for v in sorted(deprecated):
            self.gen.fact(fn.pkg_fact(pkg.name, fn.deprecated_version(v)))

    def spec_versions(self, spec):
        """Return list of clauses expressing spec's version constraints."""
        spec = specify(spec)
        msg = "Internal Error: spec with no name occured. Please report to the spack maintainers."
        assert spec.name, msg

        if spec.concrete:
            return [fn.attr("version", spec.name, spec.version)]

        if spec.versions == vn.any_version:
            return []

        # record all version constraints for later
        self.version_constraints.add((spec.name, spec.versions))
        return [fn.attr("node_version_satisfies", spec.name, spec.versions)]

    def target_ranges(self, spec, single_target_fn):
        target = spec.architecture.target

        # Check if the target is a concrete target
        if str(target) in spack.vendor.archspec.cpu.TARGETS:
            return [single_target_fn(spec.name, target)]

        self.target_constraints.add(target)
        return [fn.attr("node_target_satisfies", spec.name, target)]

    def conflict_rules(self, pkg):
        for when_spec, conflict_specs in pkg.conflicts.items():
            when_spec_msg = f"conflict constraint {str(when_spec)}"
            when_spec_id = self.condition(when_spec, required_name=pkg.name, msg=when_spec_msg)

            for conflict_spec, conflict_msg in conflict_specs:
                conflict_spec = spack.spec.Spec(conflict_spec)
                if conflict_msg is None:
                    conflict_msg = f"{pkg.name}: "
                    if when_spec == spack.spec.Spec():
                        conflict_msg += f"conflicts with '{conflict_spec}'"
                    else:
                        conflict_msg += f"'{conflict_spec}' conflicts with '{when_spec}'"

                spec_for_msg = conflict_spec
                if conflict_spec == spack.spec.Spec():
                    spec_for_msg = spack.spec.Spec(pkg.name)
                conflict_spec_msg = f"conflict is triggered when {str(spec_for_msg)}"
                conflict_spec_id = self.condition(
                    conflict_spec,
                    required_name=conflict_spec.name or pkg.name,
                    msg=conflict_spec_msg,
                )
                self.gen.fact(
                    fn.pkg_fact(
                        pkg.name, fn.conflict(conflict_spec_id, when_spec_id, conflict_msg)
                    )
                )
                self.gen.newline()

    def config_compatible_os(self):
        """Facts about compatible os's specified in configs"""
        self.gen.h2("Compatible OS from concretizer config file")
        os_data = spack.config.get("concretizer:os_compatible", {})
        for recent, reusable in os_data.items():
            for old in reusable:
                self.gen.fact(fn.os_compatible(recent, old))
                self.gen.newline()

    def package_requirement_rules(self, pkg):
        self.emit_facts_from_requirement_rules(self.requirement_parser.rules(pkg))

    def pkg_rules(self, pkg, tests):
        pkg = self.pkg_class(pkg)

        # Namespace of the package
        self.gen.fact(fn.pkg_fact(pkg.name, fn.namespace(pkg.namespace)))

        # versions
        self.pkg_version_rules(pkg)
        self.gen.newline()

        # variants
        self.variant_rules(pkg)

        # conflicts
        self.conflict_rules(pkg)

        # virtuals
        self.package_provider_rules(pkg)

        # dependencies
        self.package_dependencies_rules(pkg)

        # splices
        if self.enable_splicing:
            self.package_splice_rules(pkg)

        self.package_requirement_rules(pkg)

        # trigger and effect tables
        self.trigger_rules()
        self.effect_rules()

    def trigger_rules(self):
        """Flushes all the trigger rules collected so far, and clears the cache."""
        if not self._trigger_cache:
            return

        self.gen.h2("Trigger conditions")
        for name in self._trigger_cache:
            cache = self._trigger_cache[name]
            for (spec_str, _), (trigger_id, requirements) in cache.items():
                self.gen.fact(fn.pkg_fact(name, fn.trigger_id(trigger_id)))
                self.gen.fact(fn.pkg_fact(name, fn.trigger_msg(spec_str)))
                for predicate in requirements:
                    self.gen.fact(fn.condition_requirement(trigger_id, *predicate.args))
                self.gen.newline()
        self._trigger_cache.clear()

    def effect_rules(self):
        """Flushes all the effect rules collected so far, and clears the cache."""
        if not self._effect_cache:
            return

        self.gen.h2("Imposed requirements")
        for name in sorted(self._effect_cache):
            cache = self._effect_cache[name]
            for (spec_str, _), (effect_id, requirements) in cache.items():
                self.gen.fact(fn.pkg_fact(name, fn.effect_id(effect_id)))
                self.gen.fact(fn.pkg_fact(name, fn.effect_msg(spec_str)))
                for predicate in requirements:
                    self.gen.fact(fn.imposed_constraint(effect_id, *predicate.args))
                self.gen.newline()
        self._effect_cache.clear()

    def define_variant(
        self,
        pkg: Type[spack.package_base.PackageBase],
        name: str,
        when: spack.spec.Spec,
        variant_def: vt.Variant,
    ):
        pkg_fact = lambda f: self.gen.fact(fn.pkg_fact(pkg.name, f))

        # Every variant id has a unique definition (conditional or unconditional), and
        # higher variant id definitions take precedence when variants intersect.
        vid = next(self._id_counter)

        # used to find a variant id from its variant definition (for variant values on specs)
        self.variant_ids_by_def_id[id(variant_def)] = vid

        if when == spack.spec.Spec():
            # unconditional variant
            pkg_fact(fn.variant_definition(name, vid))
        else:
            # conditional variant
            msg = f"Package {pkg.name} has variant '{name}' when {when}"
            cond_id = self.condition(when, required_name=pkg.name, msg=msg)
            pkg_fact(fn.variant_condition(name, vid, cond_id))

        # record type so we can construct the variant when we read it back in
        self.gen.fact(fn.variant_type(vid, variant_def.variant_type.string))

        if variant_def.sticky:
            pkg_fact(fn.variant_sticky(vid))

        # define defaults for this variant definition
        if variant_def.multi:
            for val in sorted(variant_def.make_default().values):
                pkg_fact(fn.variant_default_value_from_package_py(vid, val))
        else:
            pkg_fact(fn.variant_default_value_from_package_py(vid, variant_def.default))

        # define possible values for this variant definition
        values = variant_def.values
        if values is None:
            values = []

        elif isinstance(values, vt.DisjointSetsOfValues):
            union = set()
            for sid, s in enumerate(sorted(values.sets)):
                for value in sorted(s):
                    pkg_fact(fn.variant_value_from_disjoint_sets(vid, value, sid))
                union.update(s)
            values = union

        # ensure that every variant has at least one possible value.
        if not values:
            values = [variant_def.default]

        for value in sorted(values):
            pkg_fact(fn.variant_possible_value(vid, value))

            # we're done here for unconditional values
            if not isinstance(value, vt.ConditionalValue):
                continue

            # make a spec indicating whether the variant has this conditional value
            variant_has_value = spack.spec.Spec()
            variant_has_value.variants[name] = vt.VariantValue(
                vt.VariantType.MULTI, name, (value.value,)
            )

            if value.when:
                # the conditional value is always "possible", but it imposes its when condition as
                # a constraint if the conditional value is taken. This may seem backwards, but it
                # ensures that the conditional can only occur when its condition holds.
                self.condition(
                    required_spec=variant_has_value,
                    imposed_spec=value.when,
                    required_name=pkg.name,
                    imposed_name=pkg.name,
                    msg=f"{pkg.name} variant {name} has value '{value.value}' when {value.when}",
                )
            else:
                vstring = f"{name}='{value.value}'"

                # We know the value is never allowed statically (when was None), but we can't just
                # ignore it b/c it could come in as a possible value and we need a good error msg.
                # So, it's a conflict -- if the value is somehow used, it'll trigger an error.
                trigger_id = self.condition(
                    variant_has_value,
                    required_name=pkg.name,
                    msg=f"invalid variant value: {vstring}",
                )
                constraint_id = self.condition(
                    spack.spec.Spec(),
                    required_name=pkg.name,
                    msg="empty (total) conflict constraint",
                )
                msg = f"variant value {vstring} is conditionally disabled"
                pkg_fact(fn.conflict(trigger_id, constraint_id, msg))

        self.gen.newline()

    def define_auto_variant(self, name: str, multi: bool):
        self.gen.h3(f"Special variant: {name}")
        vid = next(self._id_counter)
        self.gen.fact(fn.auto_variant(name, vid))
        self.gen.fact(
            fn.variant_type(
                vid, vt.VariantType.MULTI.value if multi else vt.VariantType.SINGLE.value
            )
        )

    def variant_rules(self, pkg: Type[spack.package_base.PackageBase]):
        for name in pkg.variant_names():
            self.gen.h3(f"Variant {name} in package {pkg.name}")
            for when, variant_def in pkg.variant_definitions(name):
                self.define_variant(pkg, name, when, variant_def)

    def _get_condition_id(
        self,
        named_cond: spack.spec.Spec,
        cache: ConditionSpecCache,
        body: bool,
        context: ConditionIdContext,
    ) -> int:
        """Get the id for one half of a condition (either a trigger or an imposed constraint).

        Construct a key from the condition spec and any associated transformation, and
        cache the ASP functions that they imply. The saved functions will be output
        later in ``trigger_rules()`` and ``effect_rules()``.

        Returns:
            The id of the cached trigger or effect.

        """
        pkg_cache = cache[named_cond.name]

        named_cond_key = (str(named_cond), context.transform)
        result = pkg_cache.get(named_cond_key)
        if result:
            return result[0]

        cond_id = next(self._id_counter)
        requirements = self.spec_clauses(named_cond, body=body, context=context)
        if context.transform:
            requirements = context.transform(named_cond, requirements)
        pkg_cache[named_cond_key] = (cond_id, requirements)

        return cond_id

    def _condition_clauses(
        self,
        required_spec: spack.spec.Spec,
        imposed_spec: Optional[spack.spec.Spec] = None,
        *,
        required_name: Optional[str] = None,
        imposed_name: Optional[str] = None,
        msg: Optional[str] = None,
        context: Optional[ConditionContext] = None,
    ) -> Tuple[List[AspFunction], int]:
        clauses = []
        required_name = required_spec.name or required_name
        if not required_name:
            raise ValueError(f"Must provide a name for anonymous condition: '{required_spec}'")

        if not context:
            context = ConditionContext()
            context.transform_imposed = remove_facts("node", "virtual_node")

        if imposed_spec:
            imposed_name = imposed_spec.name or imposed_name
            if not imposed_name:
                raise ValueError(f"Must provide a name for imposed constraint: '{imposed_spec}'")

        with named_spec(required_spec, required_name), named_spec(imposed_spec, imposed_name):
            # Check if we can emit the requirements before updating the condition ID counter.
            # In this way, if a condition can't be emitted but the exception is handled in the
            # caller, we won't emit partial facts.

            condition_id = next(self._id_counter)
            requirement_context = context.requirement_context()
            trigger_id = self._get_condition_id(
                required_spec, cache=self._trigger_cache, body=True, context=requirement_context
            )
            clauses.append(fn.pkg_fact(required_spec.name, fn.condition(condition_id)))
            clauses.append(fn.condition_reason(condition_id, msg))
            clauses.append(
                fn.pkg_fact(required_spec.name, fn.condition_trigger(condition_id, trigger_id))
            )
            if not imposed_spec:
                return clauses, condition_id

            impose_context = context.impose_context()
            effect_id = self._get_condition_id(
                imposed_spec, cache=self._effect_cache, body=False, context=impose_context
            )
            clauses.append(
                fn.pkg_fact(required_spec.name, fn.condition_effect(condition_id, effect_id))
            )

            return clauses, condition_id

    def condition(
        self,
        required_spec: spack.spec.Spec,
        imposed_spec: Optional[spack.spec.Spec] = None,
        *,
        required_name: Optional[str] = None,
        imposed_name: Optional[str] = None,
        msg: Optional[str] = None,
        context: Optional[ConditionContext] = None,
    ) -> int:
        """Generate facts for a dependency or virtual provider condition.

        Arguments:
            required_spec: the constraints that triggers this condition
            imposed_spec: the constraints that are imposed when this condition is triggered
            required_name: name for ``required_spec``
                (required if required_spec is anonymous, ignored if not)
            imposed_name: name for ``imposed_spec``
                (required if imposed_spec is anonymous, ignored if not)
            msg: description of the condition
            context: if provided, indicates how to modify the clause-sets for the required/imposed
                specs based on the type of constraint they are generated for (e.g. ``depends_on``)
        Returns:
            int: id of the condition created by this function
        """
        clauses, condition_id = self._condition_clauses(
            required_spec=required_spec,
            imposed_spec=imposed_spec,
            required_name=required_name,
            imposed_name=imposed_name,
            msg=msg,
            context=context,
        )
        for clause in clauses:
            self.gen.fact(clause)

        return condition_id

    def impose(self, condition_id, imposed_spec, node=True, body=False):
        imposed_constraints = self.spec_clauses(imposed_spec, body=body)
        for pred in imposed_constraints:
            # imposed "node"-like conditions are no-ops
            if not node and pred.args[0] in ("node", "virtual_node"):
                continue
            self.gen.fact(fn.imposed_constraint(condition_id, *pred.args))

    def package_provider_rules(self, pkg):
        for vpkg_name in pkg.provided_virtual_names():
            if vpkg_name not in self.possible_virtuals:
                continue
            self.gen.fact(fn.pkg_fact(pkg.name, fn.possible_provider(vpkg_name)))

        for when, provided in pkg.provided.items():
            for vpkg in sorted(provided):
                if vpkg.name not in self.possible_virtuals:
                    continue

                msg = f"{pkg.name} provides {vpkg} when {when}"
                condition_id = self.condition(when, vpkg, required_name=pkg.name, msg=msg)
                self.gen.fact(
                    fn.pkg_fact(when.name, fn.provider_condition(condition_id, vpkg.name))
                )
            self.gen.newline()

        for when, sets_of_virtuals in pkg.provided_together.items():
            condition_id = self.condition(
                when, required_name=pkg.name, msg="Virtuals are provided together"
            )
            for set_id, virtuals_together in enumerate(sorted(sets_of_virtuals)):
                for name in sorted(virtuals_together):
                    self.gen.fact(
                        fn.pkg_fact(pkg.name, fn.provided_together(condition_id, set_id, name))
                    )
            self.gen.newline()

    def package_dependencies_rules(self, pkg):
        """Translate ``depends_on`` directives into ASP logic."""
        for cond, deps_by_name in sorted(pkg.dependencies.items()):
            for _, dep in sorted(deps_by_name.items()):
                depflag = dep.depflag
                # Skip test dependencies if they're not requested
                if not self.tests:
                    depflag &= ~dt.TEST

                # ... or if they are requested only for certain packages
                elif not isinstance(self.tests, bool) and pkg.name not in self.tests:
                    depflag &= ~dt.TEST

                # if there are no dependency types to be considered
                # anymore, don't generate the dependency
                if not depflag:
                    continue

                msg = f"{pkg.name} depends on {dep.spec}"
                if cond != spack.spec.Spec():
                    msg += f" when {cond}"
                else:
                    pass

                def track_dependencies(input_spec, requirements):
                    return requirements + [fn.attr("track_dependencies", input_spec.name)]

                def dependency_holds(input_spec, requirements):
                    result = remove_facts("node", "virtual_node")(input_spec, requirements) + [
                        fn.attr(
                            "dependency_holds", pkg.name, input_spec.name, dt.flag_to_string(t)
                        )
                        for t in dt.ALL_FLAGS
                        if t & depflag
                    ]
                    if input_spec.name not in pkg.extendees:
                        return result
                    return result + [fn.attr("extends", pkg.name, input_spec.name)]

                context = ConditionContext()
                context.source = ConstraintOrigin.append_type_suffix(
                    pkg.name, ConstraintOrigin.DEPENDS_ON
                )
                context.transform_required = track_dependencies
                context.transform_imposed = dependency_holds

                self.condition(cond, dep.spec, required_name=pkg.name, msg=msg, context=context)

                self.gen.newline()

    def _gen_match_variant_splice_constraints(
        self,
        pkg,
        cond_spec: spack.spec.Spec,
        splice_spec: spack.spec.Spec,
        hash_asp_var: "AspVar",
        splice_node,
        match_variants: List[str],
    ):
        # If there are no variants to match, no constraints are needed
        variant_constraints = []
        for i, variant_name in enumerate(match_variants):
            vari_defs = pkg.variant_definitions(variant_name)
            # the spliceable config of the package always includes the variant
            if vari_defs != [] and any(cond_spec.satisfies(s) for (s, _) in vari_defs):
                variant = vari_defs[0][1]
                if variant.multi:
                    continue  # cannot automatically match multi-valued variants
                value_var = AspVar(f"VariValue{i}")
                attr_constraint = fn.attr("variant_value", splice_node, variant_name, value_var)
                hash_attr_constraint = fn.hash_attr(
                    hash_asp_var, "variant_value", splice_spec.name, variant_name, value_var
                )
                variant_constraints.append(attr_constraint)
                variant_constraints.append(hash_attr_constraint)
        return variant_constraints

    def package_splice_rules(self, pkg):
        self.gen.h2("Splice rules")
        for i, (cond, (spec_to_splice, match_variants)) in enumerate(
            sorted(pkg.splice_specs.items())
        ):
            with named_spec(cond, pkg.name):
                self.version_constraints.add((cond.name, cond.versions))
                self.version_constraints.add((spec_to_splice.name, spec_to_splice.versions))
                hash_var = AspVar("Hash")
                splice_node = fn.node(AspVar("NID"), cond.name)
                when_spec_attrs = [
                    fn.attr(c.args[0], splice_node, *(c.args[2:]))
                    for c in self.spec_clauses(cond, body=True, required_from=None)
                    if c.args[0] != "node"
                ]
                splice_spec_hash_attrs = [
                    fn.hash_attr(hash_var, *(c.args))
                    for c in self.spec_clauses(spec_to_splice, body=True, required_from=None)
                    if c.args[0] != "node"
                ]
                if match_variants is None:
                    variant_constraints = []
                elif match_variants == "*":
                    filt_match_variants = set()
                    for map in pkg.variants.values():
                        for k in map:
                            filt_match_variants.add(k)
                    filt_match_variants = sorted(filt_match_variants)
                    variant_constraints = self._gen_match_variant_splice_constraints(
                        pkg, cond, spec_to_splice, hash_var, splice_node, filt_match_variants
                    )
                else:
                    if any(
                        v in cond.variants or v in spec_to_splice.variants for v in match_variants
                    ):
                        raise spack.error.PackageError(
                            "Overlap between match_variants and explicitly set variants"
                        )
                    variant_constraints = self._gen_match_variant_splice_constraints(
                        pkg, cond, spec_to_splice, hash_var, splice_node, match_variants
                    )

                rule_head = fn.abi_splice_conditions_hold(
                    i, splice_node, spec_to_splice.name, hash_var
                )
                rule_body_components = (
                    [
                        # splice_set_fact,
                        fn.attr("node", splice_node),
                        fn.installed_hash(spec_to_splice.name, hash_var),
                    ]
                    + when_spec_attrs
                    + splice_spec_hash_attrs
                    + variant_constraints
                )
                rule_body = ",\n  ".join(str(r) for r in rule_body_components)
                rule = f"{rule_head} :-\n  {rule_body}."
                self.gen.append(rule)

            self.gen.newline()

    def virtual_requirements_and_weights(self):
        virtual_preferences = spack.config.CONFIG.get("packages:all:providers", {})

        self.gen.h1("Virtual requirements and weights")
        for virtual_str in sorted(self.possible_virtuals):
            self.gen.newline()
            self.gen.h2(f"Virtual: {virtual_str}")
            self.gen.fact(fn.virtual(virtual_str))

            rules = self.requirement_parser.rules_from_virtual(virtual_str)
            if not rules and virtual_str not in virtual_preferences:
                continue

            required, preferred, removed = [], [], set()
            for rule in rules:
                # We don't deal with conditional requirements
                if rule.condition != spack.spec.Spec():
                    continue

                if rule.origin == RequirementOrigin.PREFER_YAML:
                    preferred.extend(x.name for x in rule.requirements if x.name)
                elif rule.origin == RequirementOrigin.REQUIRE_YAML:
                    required.extend(x.name for x in rule.requirements if x.name)
                elif rule.origin == RequirementOrigin.CONFLICT_YAML:
                    conflict_spec = rule.requirements[0]
                    # For conflicts, we take action only if just a name is used
                    if spack.spec.Spec(conflict_spec.name).satisfies(conflict_spec):
                        removed.add(conflict_spec.name)

            current_preferences = required + preferred + virtual_preferences.get(virtual_str, [])
            current_preferences = [x for x in current_preferences if x not in removed]
            for i, provider in enumerate(spack.llnl.util.lang.dedupe(current_preferences)):
                provider_name = spack.spec.Spec(provider).name
                self.gen.fact(fn.provider_weight_from_config(virtual_str, provider_name, i))
            self.gen.newline()

            if rules:
                self.emit_facts_from_requirement_rules(rules)
                self.trigger_rules()
                self.effect_rules()

    def emit_facts_from_requirement_rules(self, rules: List[RequirementRule]):
        """Generate facts to enforce requirements.

        Args:
            rules: rules for which we want facts to be emitted
        """
        for requirement_grp_id, rule in enumerate(rules):
            virtual = rule.kind == RequirementKind.VIRTUAL

            pkg_name, policy, requirement_grp = rule.pkg_name, rule.policy, rule.requirements
            requirement_weight = 0

            # Write explicitly if a requirement is conditional or not
            if rule.condition != spack.spec.Spec():
                msg = f"condition to activate requirement {requirement_grp_id}"
                try:
                    main_condition_id = self.condition(
                        rule.condition, required_name=pkg_name, msg=msg
                    )
                except Exception as e:
                    if rule.kind != RequirementKind.DEFAULT:
                        raise RuntimeError(
                            "cannot emit requirements for the solver: " + str(e)
                        ) from e
                    continue

                self.gen.fact(
                    fn.requirement_conditional(pkg_name, requirement_grp_id, main_condition_id)
                )

            self.gen.fact(fn.requirement_group(pkg_name, requirement_grp_id))
            self.gen.fact(fn.requirement_policy(pkg_name, requirement_grp_id, policy))
            if rule.message:
                self.gen.fact(fn.requirement_message(pkg_name, requirement_grp_id, rule.message))
            self.gen.newline()

            for input_spec in requirement_grp:
                spec = spack.spec.Spec(input_spec)
                spec.replace_hash()
                if not spec.name:
                    spec.name = pkg_name
                spec.attach_git_version_lookup()

                when_spec = spec
                if virtual and spec.name != pkg_name:
                    when_spec = spack.spec.Spec(f"^[virtuals={pkg_name}] {spec}")

                try:
                    context = ConditionContext()
                    context.source = ConstraintOrigin.append_type_suffix(
                        pkg_name, ConstraintOrigin.REQUIRE
                    )
                    context.wrap_node_requirement = True
                    if not virtual:
                        context.transform_required = remove_facts("depends_on")
                        context.transform_imposed = remove_facts(
                            "node", "virtual_node", "depends_on"
                        )
                    # else: for virtuals we want to emit "node" and
                    # "virtual_node" in imposed specs

                    member_id = self.condition(
                        required_spec=when_spec,
                        imposed_spec=spec,
                        required_name=pkg_name,
                        msg=f"{input_spec} is a requirement for package {pkg_name}",
                        context=context,
                    )

                    # Conditions don't handle conditional dependencies directly
                    # Those are handled separately here
                    self.generate_conditional_dep_conditions(spec, member_id)
                except Exception as e:
                    # Do not raise if the rule comes from the 'all' subsection, since usability
                    # would be impaired. If a rule does not apply for a specific package, just
                    # discard it.
                    if rule.kind != RequirementKind.DEFAULT:
                        raise RuntimeError(
                            "cannot emit requirements for the solver: " + str(e)
                        ) from e
                    continue

                self.gen.fact(fn.requirement_group_member(member_id, pkg_name, requirement_grp_id))
                self.gen.fact(fn.requirement_has_weight(member_id, requirement_weight))
                self.gen.newline()
                requirement_weight += 1

    def external_packages(self):
        """Facts on external packages, from packages.yaml and implicit externals."""
        self.gen.h1("External packages")
        spec_filters = []
        concretizer_yaml = spack.config.get("concretizer")
        reuse_yaml = concretizer_yaml.get("reuse")
        if isinstance(reuse_yaml, typing.Mapping):
            default_include = reuse_yaml.get("include", [])
            default_exclude = reuse_yaml.get("exclude", [])
            for source in reuse_yaml.get("from", []):
                if source["type"] != "external":
                    continue

                include = source.get("include", default_include)
                if include:
                    # Since libcs are implicit externals, we need to implicitly include them
                    include = include + self.libcs
                exclude = source.get("exclude", default_exclude)
                spec_filters.append(
                    SpecFilter(
                        factory=lambda: [],
                        is_usable=lambda x: True,
                        include=include,
                        exclude=exclude,
                    )
                )

        packages_yaml = _external_config_with_implicit_externals(spack.config.CONFIG)
        for pkg_name, data in packages_yaml.items():
            if pkg_name == "all":
                continue

            # This package is not among possible dependencies
            if pkg_name not in self.pkgs:
                continue

            # Check if the external package is buildable. If it is
            # not then "external(<pkg>)" is a fact, unless we can
            # reuse an already installed spec.
            external_buildable = data.get("buildable", True)
            externals = data.get("externals", [])
            if not external_buildable or externals:
                self.gen.h2(f"External package: {pkg_name}")

            if not external_buildable:
                self.gen.fact(fn.buildable_false(pkg_name))

            # Read a list of all the specs for this package
            candidate_specs = [
                spack.spec.parse_with_version_concrete(x["spec"]) for x in externals
            ]

            selected_externals = set()
            if spec_filters:
                for current_filter in spec_filters:
                    current_filter.factory = lambda: candidate_specs
                    selected_externals.update(current_filter.selected_specs())

            # Emit facts for externals specs. Note that "local_idx" is the index of the spec
            # in packages:<pkg_name>:externals. This means:
            #
            # packages:<pkg_name>:externals[local_idx].spec == spec
            external_versions = []
            for local_idx, spec in enumerate(candidate_specs):
                msg = f"{spec.name} available as external when satisfying {spec}"

                if any(x.satisfies(spec) for x in self.rejected_compilers):
                    tty.debug(
                        f"[{__name__}]: not considering {spec} as external, since "
                        f"it's a non-working compiler"
                    )
                    continue

                if spec_filters and spec not in selected_externals:
                    continue

                if not spec.versions.concrete:
                    warnings.warn(f"cannot use the external spec {spec}: needs a concrete version")
                    continue

                def external_requirement(input_spec, requirements):
                    result = []
                    for asp_fn in requirements:
                        if asp_fn.args[0] == "depends_on":
                            continue
                        if asp_fn.args[1] != input_spec.name:
                            continue
                        result.append(asp_fn)
                    return result

                def external_imposition(input_spec, requirements):
                    result = []
                    for asp_fn in requirements:
                        if asp_fn.args[0] == "depends_on":
                            continue
                        elif asp_fn.args[0] == "direct_dependency":
                            asp_fn.args = "external_build_requirement", *asp_fn.args[1:]
                        if asp_fn.args[1] != input_spec.name:
                            continue
                        result.append(asp_fn)
                    result.append(fn.attr("external_conditions_hold", input_spec.name, local_idx))
                    return result

                try:
                    context = ConditionContext()
                    context.transform_required = external_requirement
                    context.transform_imposed = external_imposition
                    self.condition(spec, spec, msg=msg, context=context)
                except (spack.error.SpecError, RuntimeError) as e:
                    warnings.warn(f"while setting up external spec {spec}: {e}")
                    continue
                external_versions.append((spec.version, local_idx))
                self.possible_versions[spec.name].add(spec.version)
                self.gen.newline()

            # Order the external versions to prefer more recent versions
            # even if specs in packages.yaml are not ordered that way
            external_versions = [
                (v, idx, external_id)
                for idx, (v, external_id) in enumerate(sorted(external_versions, reverse=True))
            ]
            for version, idx, external_id in external_versions:
                self.declared_versions[pkg_name].append(
                    DeclaredVersion(version=version, idx=idx, origin=Provenance.EXTERNAL)
                )

            self.trigger_rules()
            self.effect_rules()

    def preferred_variants(self, pkg_name):
        """Facts on concretization preferences, as read from packages.yaml"""
        preferences = spack.package_prefs.PackagePrefs
        preferred_variants = preferences.preferred_variants(pkg_name)
        if not preferred_variants:
            return

        self.gen.h2(f"Package preferences: {pkg_name}")

        for variant_name in sorted(preferred_variants):
            variant = preferred_variants[variant_name]

            # perform validation of the variant and values
            try:
                variant_defs = vt.prevalidate_variant_value(self.pkg_class(pkg_name), variant)
            except (vt.InvalidVariantValueError, KeyError, ValueError) as e:
                tty.debug(
                    f"[SETUP]: rejected {str(variant)} as a preference for {pkg_name}: {str(e)}"
                )
                continue

            for value in variant.values:
                for variant_def in variant_defs:
                    self.variant_values_from_specs.add((pkg_name, id(variant_def), value))
                self.gen.fact(
                    fn.variant_default_value_from_packages_yaml(pkg_name, variant.name, value)
                )

    def target_preferences(self):
        key_fn = spack.package_prefs.PackagePrefs("all", "target")

        if not self.target_specs_cache:
            self.target_specs_cache = [
                spack.spec.Spec("target={0}".format(target_name))
                for _, target_name in self.default_targets
            ]

        package_targets = self.target_specs_cache[:]
        package_targets.sort(key=key_fn)
        for i, preferred in enumerate(package_targets):
            self.gen.fact(fn.target_weight(str(preferred.architecture.target), i))

    def spec_clauses(
        self,
        spec: spack.spec.Spec,
        *,
        body: bool = False,
        transitive: bool = True,
        expand_hashes: bool = False,
        concrete_build_deps=False,
        include_runtimes=False,
        required_from: Optional[str] = None,
        context: Optional[SourceContext] = None,
    ) -> List[AspFunction]:
        """Wrap a call to ``_spec_clauses()`` into a try/except block with better error handling.

        Arguments are as for ``_spec_clauses()`` except ``required_from``.

        Arguments:
            required_from: name of package that caused this call.
        """
        try:
            clauses = self._spec_clauses(
                spec,
                body=body,
                transitive=transitive,
                expand_hashes=expand_hashes,
                concrete_build_deps=concrete_build_deps,
                include_runtimes=include_runtimes,
                context=context,
            )
        except RuntimeError as exc:
            msg = str(exc)
            if required_from:
                msg += f" [required from package '{required_from}']"
            raise RuntimeError(msg)
        return clauses

    def _spec_clauses(
        self,
        spec: spack.spec.Spec,
        *,
        body: bool = False,
        transitive: bool = True,
        expand_hashes: bool = False,
        concrete_build_deps: bool = False,
        include_runtimes: bool = False,
        context: Optional[SourceContext] = None,
        seen: Optional[Set[int]] = None,
    ) -> List[AspFunction]:
        """Return a list of clauses for a spec mandates are true.

        Arguments:
            spec: the spec to analyze
            body: if True, generate clauses to be used in rule bodies (final values) instead
                of rule heads (setters).
            transitive: if False, don't generate clauses from dependencies (default True)
            expand_hashes: if True, descend into hashes of concrete specs (default False)
            concrete_build_deps: if False, do not include pure build deps of concrete specs
                (as they have no effect on runtime constraints)
            include_runtimes: generate full dependency clauses from runtime libraries that
                are ommitted from the solve.
            context: tracks what constraint this clause set is generated for (e.g. a
                ``depends_on`` constraint in a package.py file)
            seen: set of ids of specs that have already been processed (for internal use only)

        Normally, if called with ``transitive=True``, ``spec_clauses()`` just generates
        hashes for the dependency requirements of concrete specs. If ``expand_hashes``
        is ``True``, we'll *also* output all the facts implied by transitive hashes,
        which are redundant during a solve but useful outside of one (e.g.,
        for spec ``diff``).
        """
        clauses = []
        seen = seen if seen is not None else set()
        seen.add(id(spec))

        f: Union[Type[_Head], Type[_Body]] = _Body if body else _Head

        if spec.name:
            clauses.append(
                f.node(spec.name)
                if not spack.repo.PATH.is_virtual(spec.name)
                else f.virtual_node(spec.name)
            )
        if spec.namespace:
            clauses.append(f.namespace(spec.name, spec.namespace))

        clauses.extend(self.spec_versions(spec))

        # seed architecture at the root (we'll propagate later)
        # TODO: use better semantics.
        arch = spec.architecture
        if arch:
            if arch.platform:
                clauses.append(f.node_platform(spec.name, arch.platform))
            if arch.os:
                clauses.append(f.node_os(spec.name, arch.os))
            if arch.target:
                clauses.extend(self.target_ranges(spec, f.node_target))

        # variants
        for vname, variant in sorted(spec.variants.items()):
            # TODO: variant="*" means 'variant is defined to something', which used to
            # be meaningless in concretization, as all variants had to be defined. But
            # now that variants can be conditional, it should force a variant to exist.
            if not variant.values:
                continue

            for value in variant.values:
                # ensure that the value *can* be valid for the spec
                if spec.name and not spec.concrete and not spack.repo.PATH.is_virtual(spec.name):
                    variant_defs = vt.prevalidate_variant_value(
                        self.pkg_class(spec.name), variant, spec
                    )

                    # Record that that this is a valid possible value. Accounts for
                    # int/str/etc., where valid values can't be listed in the package
                    for variant_def in variant_defs:
                        self.variant_values_from_specs.add((spec.name, id(variant_def), value))

                if variant.propagate:
                    clauses.append(f.propagate(spec.name, fn.variant_value(vname, value)))
                    if self.pkg_class(spec.name).has_variant(vname):
                        clauses.append(f.variant_value(spec.name, vname, value))
                else:
                    variant_clause = f.variant_value(spec.name, vname, value)
                    if (
                        variant.concrete
                        and variant.type == vt.VariantType.MULTI
                        and not spec.concrete
                    ):
                        if body is False:
                            variant_clause.args = (
                                f"concrete_{variant_clause.args[0]}",
                                *variant_clause.args[1:],
                            )
                        else:
                            clauses.append(
                                fn.attr("concrete_variant_request", spec.name, vname, value)
                            )
                    clauses.append(variant_clause)

        # compiler flags
        source = context.source if context else "none"
        for flag_type, flags in spec.compiler_flags.items():
            flag_group = " ".join(flags)
            for flag in flags:
                clauses.append(
                    f.node_flag(spec.name, fn.node_flag(flag_type, flag, flag_group, source))
                )
                if not spec.concrete and flag.propagate is True:
                    clauses.append(
                        f.propagate(
                            spec.name,
                            fn.node_flag(flag_type, flag, flag_group, source),
                            fn.edge_types("link", "run"),
                        )
                    )

        # Hash for concrete specs
        if spec.concrete:
            # older specs do not have package hashes, so we have to do this carefully
            package_hash = getattr(spec, "_package_hash", None)
            if package_hash:
                clauses.append(fn.attr("package_hash", spec.name, package_hash))
            clauses.append(fn.attr("hash", spec.name, spec.dag_hash()))

        edges = spec.edges_from_dependents()
        virtuals = sorted(
            {x for x in itertools.chain.from_iterable([edge.virtuals for edge in edges])}
        )
        if not body and not spec.concrete:
            for virtual in virtuals:
                clauses.append(fn.attr("provider_set", spec.name, virtual))
                clauses.append(fn.attr("virtual_node", virtual))
        else:
            for virtual in virtuals:
                clauses.append(fn.attr("virtual_on_incoming_edges", spec.name, virtual))

        # If the spec is external and concrete, we allow all the libcs on the system
        if spec.external and spec.concrete and using_libc_compatibility():
            clauses.append(fn.attr("needs_libc", spec.name))
            for libc in self.libcs:
                clauses.append(fn.attr("compatible_libc", spec.name, libc.name, libc.version))

        if not transitive:
            return clauses

        # Dependencies
        edge_clauses = []
        for dspec in spec.edges_to_dependencies():
            # Ignore conditional dependencies, they are handled by caller
            if dspec.when != spack.spec.Spec():
                continue

            dep = dspec.spec

            if spec.concrete:
                # GCC runtime is solved again by clingo, even on concrete specs, to give
                # the possibility to reuse specs built against a different runtime.
                if dep.name == "gcc-runtime":
                    edge_clauses.append(
                        fn.attr("compatible_runtime", spec.name, dep.name, f"{dep.version}:")
                    )
                    constraint_spec = spack.spec.Spec(f"{dep.name}@{dep.version}")
                    self.spec_versions(constraint_spec)
                    if not include_runtimes:
                        continue

                # libc is also solved again by clingo, but in this case the compatibility
                # is not encoded in the parent node - so we need to emit explicit facts
                if "libc" in dspec.virtuals:
                    edge_clauses.append(fn.attr("needs_libc", spec.name))
                    for libc in self.libcs:
                        if libc_is_compatible(libc, dep):
                            edge_clauses.append(
                                fn.attr("compatible_libc", spec.name, libc.name, libc.version)
                            )
                    if not include_runtimes:
                        continue

                # We know dependencies are real for concrete specs. For abstract
                # specs they just mean the dep is somehow in the DAG.
                for dtype in dt.ALL_FLAGS:
                    if not dspec.depflag & dtype:
                        continue
                    # skip build dependencies of already-installed specs
                    if concrete_build_deps or dtype != dt.BUILD:
                        edge_clauses.append(
                            fn.attr("depends_on", spec.name, dep.name, dt.flag_to_string(dtype))
                        )
                        for virtual_name in dspec.virtuals:
                            edge_clauses.append(
                                fn.attr("virtual_on_edge", spec.name, dep.name, virtual_name)
                            )
                            edge_clauses.append(fn.attr("virtual_node", virtual_name))

                # imposing hash constraints for all but pure build deps of
                # already-installed concrete specs.
                if concrete_build_deps or dspec.depflag != dt.BUILD:
                    edge_clauses.append(fn.attr("hash", dep.name, dep.dag_hash()))
                elif not concrete_build_deps and dspec.depflag:
                    edge_clauses.append(
                        fn.attr("concrete_build_dependency", spec.name, dep.name, dep.dag_hash())
                    )
                    for virtual_name in dspec.virtuals:
                        edge_clauses.append(
                            fn.attr("virtual_on_build_edge", spec.name, dep.name, virtual_name)
                        )

            # if the spec is abstract, descend into dependencies.
            # if it's concrete, then the hashes above take care of dependency
            # constraints, but expand the hashes if asked for.
            if (not spec.concrete or expand_hashes) and id(dep) not in seen:
                dependency_clauses = self._spec_clauses(
                    dep,
                    body=body,
                    expand_hashes=expand_hashes,
                    concrete_build_deps=concrete_build_deps,
                    context=context,
                    seen=seen,
                )
                ###
                # Dependency expressed with "^"
                ###
                if not dspec.direct:
                    edge_clauses.extend(dependency_clauses)
                    continue

                ###
                # Direct dependencies expressed with "%"
                ###
                for dependency_type in dt.flag_to_tuple(dspec.depflag):
                    edge_clauses.append(
                        fn.attr("depends_on", spec.name, dep.name, dependency_type)
                    )

                # By default, wrap head of rules, unless the context says otherwise
                wrap_node_requirement = body is False
                if context and context.wrap_node_requirement is not None:
                    wrap_node_requirement = context.wrap_node_requirement

                if not wrap_node_requirement:
                    edge_clauses.extend(dependency_clauses)
                    continue

                for clause in dependency_clauses:
                    clause.name = "node_requirement"
                    edge_clauses.append(fn.attr("direct_dependency", spec.name, clause))

        clauses.extend(edge_clauses)
        return clauses

    def define_package_versions_and_validate_preferences(
        self, possible_pkgs: Set[str], *, require_checksum: bool, allow_deprecated: bool
    ):
        """Declare any versions in specs not declared in packages."""
        packages_yaml = spack.config.get("packages")
        for pkg_name in sorted(possible_pkgs):
            pkg_cls = self.pkg_class(pkg_name)

            # All the versions from the corresponding package.py file. Since concepts
            # like being a "develop" version or being preferred exist only at a
            # package.py level, sort them in this partial list here
            package_py_versions = sorted(
                pkg_cls.versions.items(), key=concretization_version_order, reverse=True
            )

            if require_checksum and pkg_cls.has_code:
                package_py_versions = [
                    x for x in package_py_versions if _is_checksummed_version(x)
                ]

            for idx, (v, version_info) in enumerate(package_py_versions):
                if version_info.get("deprecated", False):
                    self.deprecated_versions[pkg_name].add(v)
                    if not allow_deprecated:
                        continue

                self.possible_versions[pkg_name].add(v)
                self.declared_versions[pkg_name].append(
                    DeclaredVersion(version=v, idx=idx, origin=Provenance.PACKAGE_PY)
                )

            if pkg_name not in packages_yaml or "version" not in packages_yaml[pkg_name]:
                continue

            # TODO(psakiev) Need facts about versions
            # - requires_commit (associated with tag or branch)
            version_defs: List[GitOrStandardVersion] = []

            for vstr in packages_yaml[pkg_name]["version"]:
                v = vn.ver(vstr)

                if isinstance(v, vn.GitVersion):
                    if not require_checksum or v.is_commit:
                        version_defs.append(v)
                else:
                    matches = [x for x in self.possible_versions[pkg_name] if x.satisfies(v)]
                    matches.sort(reverse=True)
                    if not matches:
                        raise spack.error.ConfigError(
                            f"Preference for version {v} does not match any known "
                            f"version of {pkg_name} (in its package.py or any external)"
                        )
                    version_defs.extend(matches)

            for weight, vdef in enumerate(spack.llnl.util.lang.dedupe(version_defs)):
                self.declared_versions[pkg_name].append(
                    DeclaredVersion(version=vdef, idx=weight, origin=Provenance.PACKAGES_YAML)
                )
                self.possible_versions[pkg_name].add(vdef)

    def define_ad_hoc_versions_from_specs(
        self, specs, origin, *, allow_deprecated: bool, require_checksum: bool
    ):
        """Add concrete versions to possible versions from lists of CLI/dev specs."""
        for s in traverse.traverse_nodes(specs):
            # If there is a concrete version on the CLI *that we know nothing
            # about*, add it to the known versions. Use idx=0, which is the
            # best possible, so they're guaranteed to be used preferentially.
            version = s.versions.concrete

            if version is None or (any((v == version) for v in self.possible_versions[s.name])):
                continue

            if require_checksum and not _is_checksummed_git_version(version):
                raise UnsatisfiableSpecError(
                    s.format("No matching version for constraint {name}{@versions}")
                )

            if not allow_deprecated and version in self.deprecated_versions[s.name]:
                continue

            declared = DeclaredVersion(version=version, idx=0, origin=origin)
            self.declared_versions[s.name].append(declared)
            self.possible_versions[s.name].add(version)

    def _supported_targets(self, compiler_name, compiler_version, targets):
        """Get a list of which targets are supported by the compiler.

        Results are ordered most to least recent.
        """
        supported, unsupported = [], []

        for target in targets:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    target.optimization_flags(
                        compiler_name, compiler_version.dotted_numeric_string
                    )
                supported.append(target)
            except spack.vendor.archspec.cpu.UnsupportedMicroarchitecture:
                unsupported.append(target)
            except ValueError:
                unsupported.append(target)

        return supported, unsupported

    def platform_defaults(self):
        self.gen.h2("Default platform")
        platform = spack.platforms.host()
        self.gen.fact(fn.node_platform_default(platform))
        self.gen.fact(fn.allowed_platform(platform))

    def os_defaults(self, specs):
        self.gen.h2("Possible operating systems")
        platform = spack.platforms.host()

        # create set of OS's to consider
        buildable = set(platform.operating_sys.keys())

        # Consider any OS's mentioned on the command line. We need this to
        # cross-concretize in CI, and for some tests.
        # TODO: OS should really be more than just a label -- rework this.
        for spec in specs:
            if spec.architecture and spec.architecture.os:
                buildable.add(spec.architecture.os)

        # make directives for buildable OS's
        for build_os in sorted(buildable):
            self.gen.fact(fn.buildable_os(build_os))

        def keyfun(os):
            return (
                os == platform.default_os,  # prefer default
                os not in buildable,  # then prefer buildables
                os,  # then sort by name
            )

        all_oses = buildable.union(self.possible_oses)
        ordered_oses = sorted(all_oses, key=keyfun, reverse=True)

        # output the preference order of OS's for the concretizer to choose
        for i, os_name in enumerate(ordered_oses):
            self.gen.fact(fn.os(os_name, i))

    def target_defaults(self, specs):
        """Add facts about targets and target compatibility."""
        self.gen.h2("Target compatibility")

        # Add targets explicitly requested from specs
        candidate_targets = []
        for x in self.possible_graph.candidate_targets():
            if all(
                self.possible_graph.unreachable(pkg_name=pkg_name, when_spec=f"target={x}")
                for pkg_name in self.pkgs
            ):
                tty.debug(f"[{__name__}] excluding target={x}, cause no package can use it")
                continue
            candidate_targets.append(x)

        host_compatible = spack.config.CONFIG.get("concretizer:targets:host_compatible")
        for spec in specs:
            if not spec.architecture or not spec.architecture.target:
                continue

            target = spack.vendor.archspec.cpu.TARGETS.get(spec.target.name)
            if not target:
                self.target_ranges(spec, None)
                continue

            if target not in candidate_targets and not host_compatible:
                candidate_targets.append(target)
                for ancestor in target.ancestors:
                    if ancestor not in candidate_targets:
                        candidate_targets.append(ancestor)

        platform = spack.platforms.host()
        uarch = spack.vendor.archspec.cpu.TARGETS.get(platform.default)
        best_targets = {uarch.family.name}
        for compiler in self.possible_compilers:
            supported, unsupported = self._supported_targets(
                compiler.name, compiler.version, candidate_targets
            )

            for target in supported:
                best_targets.add(target.name)
                self.gen.fact(fn.target_supported(compiler.name, compiler.version, target.name))

            if supported:
                self.gen.fact(
                    fn.target_supported(compiler.name, compiler.version, uarch.family.name)
                )

            for target in unsupported:
                self.gen.fact(
                    fn.target_not_supported(compiler.name, compiler.version, target.name)
                )

            self.gen.newline()

        i = 0  # TODO compute per-target offset?
        for target in candidate_targets:
            self.gen.fact(fn.target(target.name))
            self.gen.fact(fn.target_family(target.name, target.family.name))
            self.gen.fact(fn.target_compatible(target.name, target.name))
            # Code for ancestor can run on target
            for ancestor in target.ancestors:
                self.gen.fact(fn.target_compatible(target.name, ancestor.name))

            # prefer best possible targets; weight others poorly so
            # they're not used unless set explicitly
            # these are stored to be generated as facts later offset by the
            # number of preferred targets
            if target.name in best_targets:
                self.default_targets.append((i, target.name))
                i += 1
            else:
                self.default_targets.append((100, target.name))
            self.gen.newline()

        self.default_targets = list(sorted(set(self.default_targets)))
        self.target_preferences()

    def define_version_constraints(self):
        """Define what version_satisfies(...) means in ASP logic."""

        for pkg_name, versions in sorted(self.possible_versions.items()):
            for v in versions:
                if v in self.git_commit_versions[pkg_name]:
                    sha = self.git_commit_versions[pkg_name].get(v)
                    if sha:
                        self.gen.fact(fn.pkg_fact(pkg_name, fn.version_has_commit(v, sha)))
                    else:
                        self.gen.fact(fn.pkg_fact(pkg_name, fn.version_needs_commit(v)))
        self.gen.newline()

        for pkg_name, versions in sorted(self.version_constraints):
            # generate facts for each package constraint and the version
            # that satisfies it
            for v in sorted(v for v in self.possible_versions[pkg_name] if v.satisfies(versions)):
                self.gen.fact(fn.pkg_fact(pkg_name, fn.version_satisfies(versions, v)))
            self.gen.newline()

    def collect_virtual_constraints(self):
        """Define versions for constraints on virtuals.

        Must be called before define_version_constraints().
        """
        # aggregate constraints into per-virtual sets
        constraint_map = collections.defaultdict(lambda: set())
        for pkg_name, versions in self.version_constraints:
            if not spack.repo.PATH.is_virtual(pkg_name):
                continue
            constraint_map[pkg_name].add(versions)

        # extract all the real versions mentioned in version ranges
        def versions_for(v):
            if isinstance(v, vn.StandardVersion):
                return [v]
            elif isinstance(v, vn.ClosedOpenRange):
                return [v.lo, vn._prev_version(v.hi)]
            elif isinstance(v, vn.VersionList):
                return sum((versions_for(e) for e in v), [])
            else:
                raise TypeError(f"expected version type, found: {type(v)}")

        # define a set of synthetic possible versions for virtuals, so
        # that `version_satisfies(Package, Constraint, Version)` has the
        # same semantics for virtuals as for regular packages.
        for pkg_name, versions in sorted(constraint_map.items()):
            possible_versions = set(sum([versions_for(v) for v in versions], []))
            for version in sorted(possible_versions):
                self.possible_versions[pkg_name].add(version)

    def define_compiler_version_constraints(self):
        for constraint in sorted(self.compiler_version_constraints):
            for compiler_id, compiler in enumerate(self.possible_compilers):
                if compiler.spec.satisfies(constraint):
                    self.gen.fact(
                        fn.compiler_version_satisfies(
                            constraint.name, constraint.versions, compiler_id
                        )
                    )
        self.gen.newline()

    def define_target_constraints(self):
        def _all_targets_satisfiying(single_constraint):
            allowed_targets = []

            if ":" not in single_constraint:
                return [single_constraint]

            t_min, _, t_max = single_constraint.partition(":")
            for test_target in spack.vendor.archspec.cpu.TARGETS.values():
                # Check lower bound
                if t_min and not t_min <= test_target:
                    continue

                # Check upper bound
                if t_max and not t_max >= test_target:
                    continue

                allowed_targets.append(test_target)
            return allowed_targets

        cache = {}
        for target_constraint in sorted(self.target_constraints, key=lambda x: x.name):
            # Construct the list of allowed targets for this constraint
            allowed_targets = []
            for single_constraint in str(target_constraint).split(","):
                if single_constraint not in cache:
                    cache[single_constraint] = _all_targets_satisfiying(single_constraint)
                allowed_targets.extend(cache[single_constraint])

            for target in allowed_targets:
                self.gen.fact(fn.target_satisfies(target_constraint, target))
            self.gen.newline()

    def define_variant_values(self):
        """Validate variant values from the command line.

        Add valid variant values from the command line to the possible values for
        variant definitions.

        """
        # Tell the concretizer about possible values from specs seen in spec_clauses().
        # We might want to order these facts by pkg and name if we are debugging.
        for pkg_name, variant_def_id, value in sorted(self.variant_values_from_specs):
            try:
                vid = self.variant_ids_by_def_id[variant_def_id]
            except KeyError:
                tty.debug(
                    f"[{__name__}] cannot retrieve id of the {value} variant from {pkg_name}"
                )
                continue

            self.gen.fact(fn.pkg_fact(pkg_name, fn.variant_possible_value(vid, value)))

    def register_concrete_spec(self, spec, possible):
        # tell the solver about any installed packages that could
        # be dependencies (don't tell it about the others)
        if spec.name not in possible:
            return

        try:
            # Only consider installed packages for repo we know
            spack.repo.PATH.get(spec)
        except (spack.repo.UnknownNamespaceError, spack.repo.UnknownPackageError) as e:
            tty.debug(f"[REUSE] Issues when trying to reuse {spec.short_spec}: {str(e)}")
            return

        self.reusable_and_possible.add(spec)

    def concrete_specs(self):
        """Emit facts for reusable specs"""
        for h, spec in self.reusable_and_possible.explicit_items():
            # this indicates that there is a spec like this installed
            self.gen.fact(fn.installed_hash(spec.name, h))
            # indirection layer between hash constraints and imposition to allow for splicing
            for pred in self.spec_clauses(spec, body=True, required_from=None):
                self.gen.fact(fn.hash_attr(h, *pred.args))
            self.gen.newline()
            # Declare as possible parts of specs that are not in package.py
            # - Add versions to possible versions
            # - Add OS to possible OS's

            # is traverse deterministic?
            for dep in spec.traverse():
                self.possible_versions[dep.name].add(dep.version)
                if isinstance(dep.version, vn.GitVersion):
                    self.declared_versions[dep.name].append(
                        DeclaredVersion(
                            version=dep.version, idx=0, origin=Provenance.INSTALLED_GIT_VERSION
                        )
                    )
                else:
                    self.declared_versions[dep.name].append(
                        DeclaredVersion(version=dep.version, idx=0, origin=Provenance.INSTALLED)
                    )
                self.possible_oses.add(dep.os)

    def define_concrete_input_specs(self, specs, possible):
        # any concrete specs in the input spec list
        for input_spec in specs:
            for spec in input_spec.traverse():
                if spec.concrete:
                    self.register_concrete_spec(spec, possible)

    def setup(
        self,
        specs: List[spack.spec.Spec],
        *,
        reuse: Optional[List[spack.spec.Spec]] = None,
        allow_deprecated: bool = False,
    ) -> str:
        """Generate an ASP program with relevant constraints for specs.

        This calls methods on the solve driver to set up the problem with
        facts and rules from all possible dependencies of the input
        specs, as well as constraints from the specs themselves.

        Arguments:
            specs: list of Specs to solve
            reuse: list of concrete specs that can be reused
            allow_deprecated: if True adds deprecated versions into the solve
        """
        reuse = reuse or []
        check_packages_exist(specs)
        self.gen = ProblemInstanceBuilder(randomize="SPACK_SOLVER_RANDOMIZATION" in os.environ)

        # Compute possible compilers first, so we can record which dependencies they might inject
        _ = spack.compilers.config.all_compilers(init_config=True)

        # Get compilers from buildcache only if injected through "reuse" specs
        supported_compilers = spack.compilers.config.supported_compilers()
        compilers_from_reuse = {
            x for x in reuse if x.name in supported_compilers and not x.external
        }
        candidate_compilers, self.rejected_compilers = possible_compilers(
            configuration=spack.config.CONFIG
        )
        for x in candidate_compilers:
            if x.external or x in reuse:
                continue
            reuse.append(x)
            for dep in x.traverse(root=False, deptype="run"):
                reuse.extend(dep.traverse(deptype=("link", "run")))

        candidate_compilers.update(compilers_from_reuse)
        self.possible_compilers = list(candidate_compilers)
        self.possible_compilers.sort()  # type: ignore[call-overload]

        self.gen.h1("Runtimes")
        injected_dependencies = self.define_runtime_constraints()

        node_counter = create_counter(
            specs + injected_dependencies, tests=self.tests, possible_graph=self.possible_graph
        )
        self.possible_virtuals = node_counter.possible_virtuals()
        self.pkgs = node_counter.possible_dependencies()
        self.libcs = sorted(all_libcs())  # type: ignore[type-var]

        for node in traverse.traverse_nodes(specs):
            if node.namespace is not None:
                self.explicitly_required_namespaces[node.name] = node.namespace

        self.gen.h1("Generic information")
        if using_libc_compatibility():
            for libc in self.libcs:
                self.gen.fact(fn.host_libc(libc.name, libc.version))

        if not allow_deprecated:
            self.gen.fact(fn.deprecated_versions_not_allowed())

        self.gen.newline()
        for pkg_name in spack.compilers.config.supported_compilers():
            self.gen.fact(fn.compiler_package(pkg_name))

        # Calculate develop specs
        # they will be used in addition to command line specs
        # in determining known versions/targets/os
        dev_specs: Tuple[spack.spec.Spec, ...] = ()
        env = ev.active_environment()
        if env:
            dev_specs = tuple(
                spack.spec.Spec(info["spec"]).constrained(
                    'dev_path="%s"'
                    % spack.util.path.canonicalize_path(info["path"], default_wd=env.path)
                )
                for name, info in env.dev_specs.items()
            )

        specs = tuple(specs)  # ensure compatible types to add

        self.gen.h1("Reusable concrete specs")
        self.define_concrete_input_specs(specs, self.pkgs)
        if reuse:
            self.gen.fact(fn.optimize_for_reuse())
            for reusable_spec in reuse:
                self.register_concrete_spec(reusable_spec, self.pkgs)
        self.concrete_specs()

        self.gen.h1("Generic statements on possible packages")
        node_counter.possible_packages_facts(self.gen, fn)

        self.gen.h1("Possible flags on nodes")
        for flag in spack.spec.FlagMap.valid_compiler_flags():
            self.gen.fact(fn.flag_type(flag))
        self.gen.newline()

        self.gen.h1("General Constraints")
        self.config_compatible_os()

        # architecture defaults
        self.platform_defaults()
        self.os_defaults(specs + dev_specs)
        self.target_defaults(specs + dev_specs)

        self.virtual_requirements_and_weights()
        self.external_packages()

        # TODO: make a config option for this undocumented feature
        checksummed = "SPACK_CONCRETIZER_REQUIRE_CHECKSUM" in os.environ
        self.define_package_versions_and_validate_preferences(
            self.pkgs, allow_deprecated=allow_deprecated, require_checksum=checksummed
        )
        self.define_ad_hoc_versions_from_specs(
            specs, Provenance.SPEC, allow_deprecated=allow_deprecated, require_checksum=checksummed
        )
        self.define_ad_hoc_versions_from_specs(
            dev_specs,
            Provenance.DEV_SPEC,
            allow_deprecated=allow_deprecated,
            require_checksum=checksummed,
        )
        self.validate_and_define_versions_from_requirements(
            allow_deprecated=allow_deprecated, require_checksum=checksummed
        )

        self.gen.h1("Package Constraints")
        for pkg in sorted(self.pkgs):
            self.gen.h2(f"Package rules: {pkg}")
            self.pkg_rules(pkg, tests=self.tests)
            self.preferred_variants(pkg)

        self.gen.h1("Special variants")
        self.define_auto_variant("dev_path", multi=False)
        self.define_auto_variant("commit", multi=False)
        self.define_auto_variant("patches", multi=True)

        self.gen.h1("Develop specs")
        # Inject dev_path from environment
        for ds in dev_specs:
            self.condition(spack.spec.Spec(ds.name), ds, msg=f"{ds.name} is a develop spec")
            self.trigger_rules()
            self.effect_rules()

        self.gen.h1("Spec Constraints")
        self.literal_specs(specs)

        self.gen.h1("Variant Values defined in specs")
        self.define_variant_values()

        self.gen.h1("Version Constraints")
        self.collect_virtual_constraints()
        self.define_version_constraints()

        self.gen.h1("Compiler Version Constraints")
        self.define_compiler_version_constraints()

        self.gen.h1("Target Constraints")
        self.define_target_constraints()

        self.gen.h1("Internal errors")
        self.internal_errors()

        return self.gen.value()

    def internal_errors(self):
        parent_dir = os.path.dirname(__file__)

        def visit(node):
            if ast_type(node) == clingo().ast.ASTType.Rule:
                for term in node.body:
                    if ast_type(term) == clingo().ast.ASTType.Literal:
                        if ast_type(term.atom) == clingo().ast.ASTType.SymbolicAtom:
                            name = ast_sym(term.atom).name
                            if name == "internal_error":
                                arg = ast_sym(ast_sym(term.atom).arguments[0])
                                symbol = AspFunction(name)(arg.string)
                                self.assumptions.append((parse_term(str(symbol)), True))
                                self.gen.asp_problem.append(f"{{ {symbol} }}.\n")

        path = os.path.join(parent_dir, "concretize.lp")
        parse_files([path], visit)

    def define_runtime_constraints(self) -> List[spack.spec.Spec]:
        """Define the constraints to be imposed on the runtimes, and returns a list of
        injected packages.
        """
        recorder = RuntimePropertyRecorder(self)

        for compiler in self.possible_compilers:
            try:
                compiler_cls = spack.repo.PATH.get_pkg_class(compiler.name)
            except spack.repo.UnknownPackageError:
                pass
            else:
                if hasattr(compiler_cls, "runtime_constraints"):
                    compiler_cls.runtime_constraints(spec=compiler, pkg=recorder)
                # Inject default flags for compilers
                recorder("*").default_flags(compiler)

            # FIXME (compiler as nodes): think of using isinstance(compiler_cls, WrappedCompiler)
            # Add a dependency on the compiler wrapper
            for language in ("c", "cxx", "fortran"):
                compiler_str = f"{compiler.name}@{compiler.versions}"
                recorder("*").depends_on(
                    "compiler-wrapper",
                    when=f"%[deptypes=build virtuals={language}] {compiler_str}",
                    type="build",
                    description=f"Add the compiler wrapper when using {compiler} for {language}",
                )

            if not using_libc_compatibility():
                continue

            current_libc = None
            if compiler.external or compiler.installed:
                current_libc = CompilerPropertyDetector(compiler).default_libc()
            else:
                try:
                    current_libc = compiler["libc"]
                except (KeyError, RuntimeError) as e:
                    tty.debug(f"{compiler} cannot determine libc because: {e}")

            if current_libc:
                recorder("*").depends_on(
                    "libc",
                    when=f"%[deptypes=build] {compiler_str}",
                    type="link",
                    description=f"Add libc when using {compiler}",
                )
                recorder("*").depends_on(
                    f"{current_libc.name}@={current_libc.version}",
                    when=f"%[deptypes=build] {compiler_str}",
                    type="link",
                    description=f"Libc is {current_libc} when using {compiler}",
                )

        recorder.consume_facts()
        return sorted(recorder.injected_dependencies)

    def literal_specs(self, specs):
        for spec in sorted(specs):
            self.gen.h2(f"Spec: {str(spec)}")
            condition_id = next(self._id_counter)
            trigger_id = next(self._id_counter)

            # Special condition triggered by "literal_solved"
            self.gen.fact(fn.literal(trigger_id))
            self.gen.fact(fn.pkg_fact(spec.name, fn.condition_trigger(condition_id, trigger_id)))
            self.gen.fact(fn.condition_reason(condition_id, f"{spec} requested explicitly"))

            imposed_spec_key = str(spec), None
            cache = self._effect_cache[spec.name]
            if imposed_spec_key in cache:
                effect_id, requirements = cache[imposed_spec_key]
            else:
                effect_id = next(self._id_counter)
                context = SourceContext()
                context.source = "literal"
                requirements = self.spec_clauses(spec, context=context)
            root_name = spec.name
            for clause in requirements:
                clause_name = clause.args[0]
                if clause_name == "variant_set":
                    requirements.append(
                        fn.attr("variant_default_value_from_cli", *clause.args[1:])
                    )
                elif clause_name in ("node", "virtual_node", "hash"):
                    # These facts are needed to compute the "condition_set" of the root
                    pkg_name = clause.args[1]
                    self.gen.fact(fn.mentioned_in_literal(trigger_id, root_name, pkg_name))

            requirements.append(
                fn.attr(
                    "virtual_root" if spack.repo.PATH.is_virtual(spec.name) else "root", spec.name
                )
            )
            requirements = [x for x in requirements if x.args[0] != "depends_on"]
            cache[imposed_spec_key] = (effect_id, requirements)
            self.gen.fact(fn.pkg_fact(spec.name, fn.condition_effect(condition_id, effect_id)))

            # Create subcondition with any conditional dependencies
            # self.spec_clauses does not do anything with conditional
            # dependencies
            self.generate_conditional_dep_conditions(spec, condition_id)

            if self.concretize_everything:
                self.gen.fact(fn.solve_literal(trigger_id))

        # Trigger rules are needed to allow conditional specs
        self.trigger_rules()
        self.effect_rules()

    def generate_conditional_dep_conditions(self, spec: spack.spec.Spec, condition_id: int):
        """Generate a subcondition in the trigger for any conditional dependencies.

        Dependencies are always modeled by a condition. For conditional dependencies,
        the when-spec is added as a subcondition of the trigger to ensure the dependency
        is only activated when the subcondition holds.
        """
        for dspec in spec.traverse_edges():
            # Ignore unconditional deps
            if dspec.when == spack.spec.Spec():
                continue

            # Cannot use "virtual_node" attr as key for condition
            # because reused specs do not track virtual nodes.
            # Instead, track whether the parent uses the virtual
            def virtual_handler(input_spec, requirements):
                ret = remove_facts("virtual_node")(input_spec, requirements)
                for edge in input_spec.traverse_edges(root=False, cover="edges"):
                    if spack.repo.PATH.is_virtual(edge.spec.name):
                        ret.append(fn.attr("uses_virtual", edge.parent.name, edge.spec.name))
                return ret

            context = ConditionContext()
            context.source = ConstraintOrigin.append_type_suffix(
                dspec.parent.name, ConstraintOrigin.CONDITIONAL_SPEC
            )
            # Default is to remove node-like attrs, override here
            context.transform_required = virtual_handler
            context.transform_imposed = lambda x, y: y

            try:
                subcondition_id = self.condition(
                    dspec.when,
                    spack.spec.Spec(dspec.format(unconditional=True)),
                    required_name=dspec.parent.name,
                    context=context,
                    msg=f"Conditional dependency in ^[when={dspec.when}]{dspec.spec}",
                )
                self.gen.fact(fn.subcondition(subcondition_id, condition_id))
            except vt.UnknownVariantError as e:
                # A variant in the 'when=' condition can't apply to the parent of the edge
                tty.debug(f"[{__name__}] cannot emit subcondition for {dspec.format()}: {e}")

    def validate_and_define_versions_from_requirements(
        self, *, allow_deprecated: bool, require_checksum: bool
    ):
        """If package requirements mention concrete versions that are not mentioned
        elsewhere, then we need to collect those to mark them as possible
        versions. If they are abstract and statically have no match, then we
        need to throw an error. This function assumes all possible versions are already
        registered in self.possible_versions."""
        for pkg_name, d in spack.config.get("packages").items():
            if pkg_name == "all" or "require" not in d:
                continue

            for s in traverse.traverse_nodes(self._specs_from_requires(pkg_name, d["require"])):
                name, versions = s.name, s.versions

                if name not in self.pkgs or versions == spack.version.any_version:
                    continue

                s.attach_git_version_lookup()
                v = versions.concrete

                if not v:
                    # If the version is not concrete, check it's statically concretizable. If
                    # not throw an error, which is just so that users know they need to change
                    # their config, instead of getting a hard to decipher concretization error.
                    if not any(x for x in self.possible_versions[name] if x.satisfies(versions)):
                        raise spack.error.ConfigError(
                            f"Version requirement {versions} on {pkg_name} for {name} "
                            f"cannot match any known version from package.py or externals"
                        )
                    continue

                if v in self.possible_versions[name]:
                    continue

                if not allow_deprecated and v in self.deprecated_versions[name]:
                    continue

                # If concrete an not yet defined, conditionally define it, like we do for specs
                # from the command line.
                if not require_checksum or _is_checksummed_git_version(v):
                    self.declared_versions[name].append(
                        DeclaredVersion(version=v, idx=0, origin=Provenance.PACKAGE_REQUIREMENT)
                    )
                    self.possible_versions[name].add(v)

    def _specs_from_requires(self, pkg_name, section):
        """Collect specs from a requirement rule"""
        if isinstance(section, str):
            yield _spec_with_default_name(section, pkg_name)
            return

        for spec_group in section:
            if isinstance(spec_group, str):
                yield _spec_with_default_name(spec_group, pkg_name)
                continue

            # Otherwise it is an object. The object can contain a single
            # "spec" constraint, or a list of them with "any_of" or
            # "one_of" policy.
            if "spec" in spec_group:
                yield _spec_with_default_name(spec_group["spec"], pkg_name)
                continue

            key = "one_of" if "one_of" in spec_group else "any_of"
            for s in spec_group[key]:
                yield _spec_with_default_name(s, pkg_name)

    def pkg_class(self, pkg_name: str) -> typing.Type[spack.package_base.PackageBase]:
        request = pkg_name
        if pkg_name in self.explicitly_required_namespaces:
            namespace = self.explicitly_required_namespaces[pkg_name]
            request = f"{namespace}.{pkg_name}"
        return spack.repo.PATH.get_pkg_class(request)


class _Head:
    """ASP functions used to express spec clauses in the HEAD of a rule"""

    node = fn.attr("node")
    namespace = fn.attr("namespace_set")
    virtual_node = fn.attr("virtual_node")
    node_platform = fn.attr("node_platform_set")
    node_os = fn.attr("node_os_set")
    node_target = fn.attr("node_target_set")
    variant_value = fn.attr("variant_set")
    node_flag = fn.attr("node_flag_set")
    propagate = fn.attr("propagate")


class _Body:
    """ASP functions used to express spec clauses in the BODY of a rule"""

    node = fn.attr("node")
    namespace = fn.attr("namespace")
    virtual_node = fn.attr("virtual_node")
    node_platform = fn.attr("node_platform")
    node_os = fn.attr("node_os")
    node_target = fn.attr("node_target")
    variant_value = fn.attr("variant_value")
    node_flag = fn.attr("node_flag")
    propagate = fn.attr("propagate")


class ProblemInstanceBuilder:
    """Provides an interface to construct a problem instance.

    Once all the facts and rules have been added, the problem instance can be retrieved with:

    >>> builder = ProblemInstanceBuilder()
    >>> ...
    >>> problem_instance = builder.value()

    The problem instance can be added directly to the "control" structure of clingo.

    Arguments:
        randomize: whether to randomize the order of facts to the solver. Useful for benchmarking.
    """

    def __init__(self, randomize: bool = False) -> None:
        self.randomize = randomize
        self.asp_problem: List[str] = []

    def fact(self, atom: AspFunction) -> None:
        self.asp_problem.append(f"{atom}.\n")

    def append(self, rule: str) -> None:
        self.asp_problem.append(rule)

    def title(self, header: str, char: str) -> None:
        sep = char * 76
        self.asp_problem.append(f"\n%{sep}\n% {header}\n%{sep}\n")

    def h1(self, header: str) -> None:
        self.title(header, "=")

    def h2(self, header: str) -> None:
        self.title(header, "-")

    def h3(self, header: str):
        self.asp_problem.append(f"% {header}\n")

    def newline(self):
        self.asp_problem.append("\n")

    def value(self) -> str:
        if self.randomize:
            random.shuffle(self.asp_problem)
        return "".join(self.asp_problem)


def possible_compilers(*, configuration) -> Tuple[Set["spack.spec.Spec"], Set["spack.spec.Spec"]]:
    result, rejected = set(), set()

    # Compilers defined in configuration
    for c in spack.compilers.config.all_compilers_from(configuration):
        if using_libc_compatibility() and not c_compiler_runs(c):
            rejected.add(c)
            try:
                compiler = c.extra_attributes["compilers"]["c"]
                tty.debug(
                    f"the C compiler {compiler} does not exist, or does not run correctly."
                    f" The compiler {c} will not be used during concretization."
                )
            except KeyError:
                tty.debug(f"the spec {c} does not provide a C compiler.")

            continue

        if using_libc_compatibility() and not CompilerPropertyDetector(c).default_libc():
            rejected.add(c)
            warnings.warn(
                f"cannot detect libc from {c}. The compiler will not be used "
                f"during concretization."
            )
            continue

        if c in result:
            tty.debug(f"[{__name__}] duplicate {c.long_spec} compiler found")
            continue

        result.add(c)

    # Compilers from the local store
    supported_compilers = spack.compilers.config.supported_compilers()
    for pkg_name in supported_compilers:
        result.update(spack.store.STORE.db.query(pkg_name))

    return result, rejected


FunctionTupleT = Tuple[str, Tuple[Union[str, NodeArgument], ...]]


class SpecBuilder:
    """Class with actions to rebuild a spec from ASP results."""

    #: Regex for attributes that don't need actions b/c they aren't used to construct specs.
    ignored_attributes = re.compile(
        "|".join(
            [
                r"^.*_propagate$",
                r"^.*_satisfies$",
                r"^.*_set$",
                r"^compatible_libc$",
                r"^dependency_holds$",
                r"^external_conditions_hold$",
                r"^package_hash$",
                r"^root$",
                r"^track_dependencies$",
                r"^uses_virtual$",
                r"^variant_default_value_from_cli$",
                r"^virtual_node$",
                r"^virtual_on_incoming_edges$",
                r"^virtual_root$",
            ]
        )
    )

    @staticmethod
    def make_node(*, pkg: str) -> NodeArgument:
        """Given a package name, returns the string representation of the "min_dupe_id" node in
        the ASP encoding.

        Args:
            pkg: name of a package
        """
        return NodeArgument(id="0", pkg=pkg)

    def __init__(self, specs, hash_lookup=None):
        self._specs: Dict[NodeArgument, spack.spec.Spec] = {}

        # Matches parent nodes to splice node
        self._splices: Dict[spack.spec.Spec, List[spack.solver.splicing.Splice]] = {}
        self._result = None
        self._command_line_specs = specs
        self._flag_sources: Dict[Tuple[NodeArgument, str], Set[str]] = collections.defaultdict(
            lambda: set()
        )

        # Pass in as arguments reusable specs and plug them in
        # from this dictionary during reconstruction
        self._hash_lookup = hash_lookup or ConcreteSpecsByHash()

    def hash(self, node, h):
        if node not in self._specs:
            self._specs[node] = self._hash_lookup[h]

    def node(self, node):
        if node not in self._specs:
            self._specs[node] = spack.spec.Spec(node.pkg)
            for flag_type in spack.spec.FlagMap.valid_compiler_flags():
                self._specs[node].compiler_flags[flag_type] = []

    def _arch(self, node):
        arch = self._specs[node].architecture
        if not arch:
            arch = spack.spec.ArchSpec()
            self._specs[node].architecture = arch
        return arch

    def namespace(self, node, namespace):
        self._specs[node].namespace = namespace

    def node_platform(self, node, platform):
        self._arch(node).platform = platform

    def node_os(self, node, os):
        self._arch(node).os = os

    def node_target(self, node, target):
        self._arch(node).target = target

    def variant_selected(self, node, name: str, value: str, variant_type: str, variant_id):
        spec = self._specs[node]
        variant = spec.variants.get(name)
        if not variant:
            spec.variants[name] = vt.VariantValue.from_concretizer(name, value, variant_type)
        else:
            assert variant_type == "multi", (
                f"Can't have multiple values for single-valued variant: "
                f"{node}, {name}, {value}, {variant_type}, {variant_id}"
            )
            variant.append(value)

    def version(self, node, version):
        self._specs[node].versions = vn.VersionList([vn.Version(version)])

    def node_flag(self, node, node_flag):
        self._specs[node].compiler_flags.add_flag(
            node_flag.flag_type, node_flag.flag, False, node_flag.flag_group, node_flag.source
        )

    def external_spec_selected(self, node, idx):
        """This means that the external spec and index idx has been selected for this package."""
        packages_yaml = _external_config_with_implicit_externals(spack.config.CONFIG)
        spec_info = packages_yaml[node.pkg]["externals"][int(idx)]
        self._specs[node].external_path = spec_info.get("prefix", None)
        self._specs[node].external_modules = spack.spec.Spec._format_module_list(
            spec_info.get("modules", None)
        )
        self._specs[node].extra_attributes = spec_info.get("extra_attributes", {})

        # Annotate compiler specs from externals
        external_spec = spack.spec.Spec(spec_info["spec"])
        external_spec_deps = external_spec.dependencies()
        if len(external_spec_deps) > 1:
            raise InvalidExternalError(
                f"external spec {spec_info['spec']} cannot have more than one dependency"
            )
        elif len(external_spec_deps) == 1:
            compiler_str = external_spec_deps[0]
            self._specs[node].annotations.with_compiler(spack.spec.Spec(compiler_str))

        # Packages that are external - but normally depend on python -
        # get an edge inserted to python as a post-concretization step
        package = spack.repo.PATH.get_pkg_class(self._specs[node].fullname)(self._specs[node])
        extendee_spec = package.extendee_spec
        if (
            extendee_spec
            and extendee_spec.name == "python"
            # More-general criteria like "depends on Python" pulls in things
            # we don't want to apply this logic to (in particular LLVM, which
            # is now a common external because that's how we detect Clang)
            and any([c.__name__ == "PythonExtension" for c in package.__class__.__mro__])
        ):
            candidate_python_to_attach = self._specs.get(SpecBuilder.make_node(pkg="python"))
            _attach_python_to_external(package, extendee_spec=candidate_python_to_attach)

    def depends_on(self, parent_node, dependency_node, type):
        dependency_spec = self._specs[dependency_node]
        depflag = dt.flag_from_string(type)
        self._specs[parent_node].add_dependency_edge(dependency_spec, depflag=depflag, virtuals=())

    def virtual_on_edge(self, parent_node, provider_node, virtual):
        dependencies = self._specs[parent_node].edges_to_dependencies(name=(provider_node.pkg))
        provider_spec = self._specs[provider_node]
        dependencies = [x for x in dependencies if id(x.spec) == id(provider_spec)]
        assert len(dependencies) == 1, f"{virtual}: {provider_node.pkg}"
        dependencies[0].update_virtuals(virtual)

    def reorder_flags(self):
        """For each spec, determine the order of compiler flags applied to it.

        The solver determines which flags are on nodes; this routine
        imposes order afterwards. The order is:

        1. Flags applied in compiler definitions should come first
        2. Flags applied by dependents are ordered topologically (with a
           dependency on ``traverse`` to resolve the partial order into a
           stable total order)
        3. Flags from requirements are then applied (requirements always
           come from the package and never a parent)
        4. Command-line flags should come last

        Additionally, for each source (requirements, compiler, command line, and
        dependents), flags from that source should retain their order and grouping:
        e.g. for ``y cflags="-z -a"`` ``-z`` and ``-a`` should never have any intervening
        flags inserted, and should always appear in that order.
        """
        for node, spec in self._specs.items():
            # if bootstrapping, compiler is not in config and has no flags
            flagmap_from_compiler = {
                flag_type: [x for x in values if x.source == "compiler"]
                for flag_type, values in spec.compiler_flags.items()
            }

            flagmap_from_cli = {}
            for flag_type, values in spec.compiler_flags.items():
                if not values:
                    continue

                flags = [x for x in values if x.source == "literal"]
                if not flags:
                    continue

                # For compiler flags from literal specs, reorder any flags to
                # the input order from flag.flag_group
                flagmap_from_cli[flag_type] = _reorder_flags(flags)

            for flag_type in spec.compiler_flags.valid_compiler_flags():
                ordered_flags = []

                # 1. Put compiler flags first
                from_compiler = tuple(flagmap_from_compiler.get(flag_type, []))
                extend_flag_list(ordered_flags, from_compiler)

                # 2. Add all sources (the compiler is one of them, so skip any
                # flag group that matches it exactly)
                flag_groups = set()
                for flag in self._specs[node].compiler_flags.get(flag_type, []):
                    flag_groups.add(
                        spack.spec.CompilerFlag(
                            flag.flag_group,
                            propagate=flag.propagate,
                            flag_group=flag.flag_group,
                            source=flag.source,
                        )
                    )

                # For flags that are applied by dependents, put flags from parents
                # before children; we depend on the stability of traverse() to
                # achieve a stable flag order for flags introduced in this manner.
                topo_order = list(s.name for s in spec.traverse(order="post", direction="parents"))
                lex_order = list(sorted(flag_groups))

                def _order_index(flag_group):
                    source = flag_group.source
                    # Note: if 'require: ^dependency cflags=...' is ever possible,
                    # this will topologically sort for require as well
                    type_index, pkg_source = ConstraintOrigin.strip_type_suffix(source)
                    if pkg_source in topo_order:
                        major_index = topo_order.index(pkg_source)
                        # If for x->y, x has multiple depends_on declarations that
                        # are activated, and each adds cflags to y, we fall back on
                        # alphabetical ordering to maintain a total order
                        minor_index = lex_order.index(flag_group)
                    else:
                        major_index = len(topo_order) + lex_order.index(flag_group)
                        minor_index = 0
                    return (type_index, major_index, minor_index)

                prioritized_groups = sorted(flag_groups, key=lambda x: _order_index(x))

                for grp in prioritized_groups:
                    grp_flags = tuple(
                        x for (x, y) in spack.compilers.flags.tokenize_flags(grp.flag_group)
                    )
                    if grp_flags == from_compiler:
                        continue
                    as_compiler_flags = list(
                        spack.spec.CompilerFlag(
                            x,
                            propagate=grp.propagate,
                            flag_group=grp.flag_group,
                            source=grp.source,
                        )
                        for x in grp_flags
                    )
                    extend_flag_list(ordered_flags, as_compiler_flags)

                # 3. Now put cmd-line flags last
                if flag_type in flagmap_from_cli:
                    extend_flag_list(ordered_flags, flagmap_from_cli[flag_type])

                compiler_flags = spec.compiler_flags.get(flag_type, [])
                msg = f"{set(compiler_flags)} does not equal {set(ordered_flags)}"
                assert set(compiler_flags) == set(ordered_flags), msg

                spec.compiler_flags.update({flag_type: ordered_flags})

    def deprecated(self, node: NodeArgument, version: str) -> None:
        tty.warn(f'using "{node.pkg}@{version}" which is a deprecated version')

    def splice_at_hash(
        self,
        parent_node: NodeArgument,
        splice_node: NodeArgument,
        child_name: str,
        child_hash: str,
    ):
        parent_spec = self._specs[parent_node]
        splice_spec = self._specs[splice_node]
        splice = spack.solver.splicing.Splice(
            splice_spec, child_name=child_name, child_hash=child_hash
        )
        self._splices.setdefault(parent_spec, []).append(splice)

    def build_specs(self, function_tuples: List[FunctionTupleT]) -> List[spack.spec.Spec]:

        attr_key = {
            # hash attributes are handled first, since they imply entire concrete specs
            "hash": -5,
            # node attributes are handled next, since they instantiate nodes
            "node": -4,
            # evaluated last, so all nodes are fully constructed
            "external_spec_selected": 1,
            "virtual_on_edge": 2,
        }

        # Sort functions so that directives building objects are called in the right order
        function_tuples.sort(key=lambda x: attr_key.get(x[0], 0))
        self._specs = {}
        for name, args in function_tuples:
            if SpecBuilder.ignored_attributes.match(name):
                continue

            action = getattr(self, name, None)

            # print out unknown actions so we can display them for debugging
            if not action:
                msg = f'UNKNOWN SYMBOL: attr("{name}", {", ".join(str(a) for a in args)})'
                tty.debug(msg)
                continue

            msg = (
                "Internal Error: Uncallable action found in asp.py.  Please report to the spack"
                " maintainers."
            )
            assert action and callable(action), msg

            # ignore predicates on virtual packages, as they're used for
            # solving but don't construct anything. Do not ignore error
            # predicates on virtual packages.
            if name != "error":
                node = args[0]
                assert isinstance(node, NodeArgument), (
                    f"internal solver error: expected a node, but got a {type(args[0])}. "
                    "Please report a bug at https://github.com/spack/spack/issues"
                )

                pkg = node.pkg
                if spack.repo.PATH.is_virtual(pkg):
                    continue

                # if we've already gotten a concrete spec for this pkg,
                # do not bother calling actions on it except for node_flag_source,
                # since node_flag_source is tracking information not in the spec itself
                # we also need to keep track of splicing information.
                spec = self._specs.get(node)
                if spec and spec.concrete:
                    do_not_ignore_attrs = ["node_flag_source", "splice_at_hash"]
                    if name not in do_not_ignore_attrs:
                        continue

            action(*args)

        # fix flags after all specs are constructed
        self.reorder_flags()

        # inject patches -- note that we' can't use set() to unique the
        # roots here, because the specs aren't complete, and the hash
        # function will loop forever.
        roots = [spec.root for spec in self._specs.values()]
        roots = dict((id(r), r) for r in roots)
        for root in roots.values():
            _inject_patches_variant(root)

        # Add external paths to specs with just external modules
        for s in self._specs.values():
            _ensure_external_path_if_external(s)

        for s in self._specs.values():
            _develop_specs_from_env(s, ev.active_environment())

        # check for commits must happen after all version adaptations are complete
        for s in self._specs.values():
            _specs_with_commits(s)

        # mark concrete and assign hashes to all specs in the solve
        for root in roots.values():
            root._finalize_concretization()

        # Unify hashes (this is to avoid duplicates of runtimes and compilers)
        unifier = ConcreteSpecsByHash()
        keys = list(self._specs)
        for key in keys:
            current_spec = self._specs[key]
            unifier.add(current_spec)
            self._specs[key] = unifier[current_spec.dag_hash()]

        # Only attempt to resolve automatic splices if the solver produced any
        if self._splices:
            resolved_splices = spack.solver.splicing._resolve_collected_splices(
                list(self._specs.values()), self._splices
            )
            new_specs = {}
            for node, spec in self._specs.items():
                new_specs[node] = resolved_splices.get(spec, spec)
            self._specs = new_specs

        for s in self._specs.values():
            spack.spec.Spec.ensure_no_deprecated(s)

        # Add git version lookup info to concrete Specs (this is generated for
        # abstract specs as well but the Versions may be replaced during the
        # concretization process)
        for root in self._specs.values():
            for spec in root.traverse():
                if isinstance(spec.version, vn.GitVersion):
                    spec.version.attach_lookup(
                        spack.version.git_ref_lookup.GitRefLookup(spec.fullname)
                    )

        specs = self.execute_explicit_splices()
        return specs

    def execute_explicit_splices(self):
        splice_config = spack.config.CONFIG.get("concretizer:splice:explicit", [])
        splice_triples = []
        for splice_set in splice_config:
            target = splice_set["target"]
            replacement = spack.spec.Spec(splice_set["replacement"])

            if not replacement.abstract_hash:
                location = getattr(
                    splice_set["replacement"], "_start_mark", " at unknown line number"
                )
                msg = f"Explicit splice replacement '{replacement}' does not include a hash.\n"
                msg += f"{location}\n\n"
                msg += "    Splice replacements must be specified by hash"
                raise InvalidSpliceError(msg)

            transitive = splice_set.get("transitive", False)
            splice_triples.append((target, replacement, transitive))

        specs = {}
        for key, spec in self._specs.items():
            current_spec = spec
            for target, replacement, transitive in splice_triples:
                if target in current_spec:
                    # matches root or non-root
                    # e.g. mvapich2%gcc

                    # The first iteration, we need to replace the abstract hash
                    if not replacement.concrete:
                        replacement.replace_hash()
                    current_spec = current_spec.splice(replacement, transitive)
            new_key = NodeArgument(id=key.id, pkg=current_spec.name)
            specs[new_key] = current_spec

        return specs


def _specs_with_commits(spec):
    pkg_class = spack.repo.PATH.get_pkg_class(spec.fullname)
    if not pkg_class.needs_commit(spec.version):
        return

    if isinstance(spec.version, spack.version.GitVersion):
        if "commit" not in spec.variants and spec.version.commit_sha:
            spec.variants["commit"] = vt.SingleValuedVariant("commit", spec.version.commit_sha)

    pkg_class._resolve_git_provenance(spec)

    if "commit" not in spec.variants:
        tty.warn(
            f"Unable to resolve the git commit for {spec.name}. "
            "An installation of this binary won't have complete binary provenance."
        )
        return

    # check integrity of user specified commit shas
    invalid_commit_msg = (
        f"Internal Error: {spec.name}'s assigned commit {spec.variants['commit'].value}"
        " does not meet commit syntax requirements."
    )
    assert vn.is_git_commit_sha(spec.variants["commit"].value), invalid_commit_msg


def _attach_python_to_external(
    dependent_package, extendee_spec: Optional[spack.spec.Spec] = None
) -> None:
    """
    Ensure all external python packages have a python dependency

    If another package in the DAG depends on python, we use that
    python for the dependency of the external. If not, we assume
    that the external PythonPackage is installed into the same
    directory as the python it depends on.
    """
    # TODO: Include this in the solve, rather than instantiating post-concretization
    if "python" not in dependent_package.spec:
        if extendee_spec:
            python = extendee_spec
        else:
            python = _get_external_python_for_prefix(dependent_package)
            if not python.concrete:
                repo = spack.repo.PATH.repo_for_pkg(python)
                python.namespace = repo.namespace

                # Ensure architecture information is present
                if not python.architecture:
                    host_platform = spack.platforms.host()
                    host_os = host_platform.default_operating_system()
                    host_target = host_platform.default_target()
                    python.architecture = spack.spec.ArchSpec(
                        (str(host_platform), str(host_os), str(host_target))
                    )
                else:
                    if not python.architecture.platform:
                        python.architecture.platform = spack.platforms.host()
                    platform = spack.platforms.by_name(python.architecture.platform)
                    if not python.architecture.os:
                        python.architecture.os = platform.default_operating_system()
                    if not python.architecture.target:
                        python.architecture.target = spack.vendor.archspec.cpu.host().family.name

                python.external_path = dependent_package.spec.external_path
                python._mark_concrete()
        dependent_package.spec.add_dependency_edge(
            python, depflag=dt.BUILD | dt.LINK | dt.RUN, virtuals=()
        )


def _get_external_python_for_prefix(python_package):
    """
    For an external package that extends python, find the most likely spec for the python
    it depends on.

    First search: an "installed" external that shares a prefix with this package
    Second search: a configured external that shares a prefix with this package
    Third search: search this prefix for a python package

    Returns:
        spack.spec.Spec: The external Spec for python most likely to be compatible with self.spec
    """
    python_externals_installed = [
        s
        for s in spack.store.STORE.db.query("python")
        if s.prefix == python_package.spec.external_path
    ]
    if python_externals_installed:
        return python_externals_installed[0]

    python_external_config = spack.config.get("packages:python:externals", [])
    python_externals_configured = [
        spack.spec.parse_with_version_concrete(item["spec"])
        for item in python_external_config
        if item["prefix"] == python_package.spec.external_path
    ]
    if python_externals_configured:
        return python_externals_configured[0]

    python_externals_detection = spack.detection.by_path(
        ["python"], path_hints=[python_package.spec.external_path], max_workers=1
    )

    python_externals_detected = [
        spec
        for spec in python_externals_detection.get("python", [])
        if spec.external_path == python_package.spec.external_path
    ]
    python_externals_detected = [
        spack.spec.parse_with_version_concrete(str(x)) for x in python_externals_detected
    ]
    if python_externals_detected:
        return list(sorted(python_externals_detected, key=lambda x: x.version))[-1]

    raise StopIteration(
        "No external python could be detected for %s to depend on" % python_package.spec
    )


def _inject_patches_variant(root: spack.spec.Spec) -> None:
    # This dictionary will store object IDs rather than Specs as keys
    # since the Spec __hash__ will change as patches are added to them
    spec_to_patches: Dict[int, Set[spack.patch.Patch]] = {}
    for s in root.traverse():
        # After concretizing, assign namespaces to anything left.
        # Note that this doesn't count as a "change".  The repository
        # configuration is constant throughout a spack run, and
        # normalize and concretize evaluate Packages using Repo.get(),
        # which respects precedence.  So, a namespace assignment isn't
        # changing how a package name would have been interpreted and
        # we can do it as late as possible to allow as much
        # compatibility across repositories as possible.
        if s.namespace is None:
            s.namespace = spack.repo.PATH.repo_for_pkg(s.name).namespace

        if s.concrete:
            continue

        # Add any patches from the package to the spec.
        node_patches = {
            patch
            for cond, patch_list in spack.repo.PATH.get_pkg_class(s.fullname).patches.items()
            if s.satisfies(cond)
            for patch in patch_list
        }
        if node_patches:
            spec_to_patches[id(s)] = node_patches

    # Also record all patches required on dependencies by depends_on(..., patch=...)
    for dspec in root.traverse_edges(deptype=dt.ALL, cover="edges", root=False):
        if dspec.spec.concrete:
            continue

        pkg_deps = spack.repo.PATH.get_pkg_class(dspec.parent.fullname).dependencies

        edge_patches: List[spack.patch.Patch] = []
        for cond, deps_by_name in pkg_deps.items():
            dependency = deps_by_name.get(dspec.spec.name)
            if not dependency:
                continue

            if not dspec.parent.satisfies(cond):
                continue

            for pcond, patch_list in dependency.patches.items():
                if dspec.spec.satisfies(pcond):
                    edge_patches.extend(patch_list)

        if edge_patches:
            spec_to_patches.setdefault(id(dspec.spec), set()).update(edge_patches)

    for spec in root.traverse():
        if id(spec) not in spec_to_patches:
            continue

        patches = list(spec_to_patches[id(spec)])
        variant: vt.VariantValue = spec.variants.setdefault(
            "patches", vt.MultiValuedVariant("patches", ())
        )
        variant.set(*(p.sha256 for p in patches))
        # FIXME: Monkey patches variant to store patches order
        ordered_hashes = [(*p.ordering_key, p.sha256) for p in patches if p.ordering_key]
        ordered_hashes.sort()
        tty.debug(
            f"Ordered hashes [{spec.name}]: "
            + ", ".join("/".join(str(e) for e in t) for t in ordered_hashes)
        )
        setattr(
            variant, "_patches_in_order_of_appearance", [sha256 for _, _, sha256 in ordered_hashes]
        )


def _ensure_external_path_if_external(spec: spack.spec.Spec) -> None:
    if not spec.external_modules or spec.external_path:
        return

    # Get the path from the module the package can override the default
    # (this is mostly needed for Cray)
    pkg_cls = spack.repo.PATH.get_pkg_class(spec.name)
    package = pkg_cls(spec)
    spec.external_path = getattr(package, "external_prefix", None) or md.path_from_modules(
        spec.external_modules
    )


def _develop_specs_from_env(spec, env):
    dev_info = env.dev_specs.get(spec.name, {}) if env else {}
    if not dev_info:
        return

    path = spack.util.path.canonicalize_path(dev_info["path"], default_wd=env.path)

    if "dev_path" in spec.variants:
        error_msg = (
            "Internal Error: The dev_path for spec {name} is not connected to a valid environment"
            "path. Please note that develop specs can only be used inside an environment"
            "These paths should be the same:\n\tdev_path:{dev_path}\n\tenv_based_path:{env_path}"
        ).format(name=spec.name, dev_path=spec.variants["dev_path"], env_path=path)

        assert spec.variants["dev_path"].value == path, error_msg
    else:
        spec.variants.setdefault("dev_path", vt.SingleValuedVariant("dev_path", path))

    assert spec.satisfies(dev_info["spec"])


class Solver:
    """This is the main external interface class for solving.

    It manages solver configuration and preferences in one place. It sets up the solve
    and passes the setup method to the driver, as well.
    """

    def __init__(self):
        self.driver = PyclingoDriver()
        self.selector = ReusableSpecsSelector(configuration=spack.config.CONFIG)

    @staticmethod
    def _check_input_and_extract_concrete_specs(
        specs: List[spack.spec.Spec],
    ) -> List[spack.spec.Spec]:
        reusable: List[spack.spec.Spec] = []
        analyzer = create_graph_analyzer()
        for root in specs:
            for s in root.traverse():
                if s.concrete:
                    reusable.append(s)
                else:
                    if spack.repo.PATH.is_virtual(s.name):
                        continue
                    # Error if direct dependencies cannot be satisfied
                    deps = {
                        edge.spec.name
                        for edge in s.edges_to_dependencies()
                        if edge.direct and edge.when == spack.spec.Spec()
                    }
                    if deps:
                        graph = analyzer.possible_dependencies(
                            s, allowed_deps=dt.ALL, transitive=False
                        )
                        deps.difference_update(graph.real_pkgs, graph.virtuals)
                        if deps:
                            start_str = f"'{root}'" if s == root else f"'{s}' in '{root}'"
                            raise UnsatisfiableSpecError(
                                f"{start_str} cannot depend on {', '.join(deps)}"
                            )

                try:
                    spack.repo.PATH.get_pkg_class(s.fullname)
                except spack.repo.UnknownPackageError:
                    raise UnsatisfiableSpecError(
                        f"cannot concretize '{root}', since '{s.name}' does not exist"
                    )

                spack.spec.Spec.ensure_valid_variants(s)
        return reusable

    def solve_with_stats(
        self,
        specs,
        out=None,
        timers=False,
        stats=False,
        tests=False,
        setup_only=False,
        allow_deprecated=False,
    ):
        """
        Concretize a set of specs and track the timing and statistics for the solve

        Arguments:
          specs (list): List of ``Spec`` objects to solve for.
          out: Optionally write the generate ASP program to a file-like object.
          timers (bool): Print out coarse timers for different solve phases.
          stats (bool): Print out detailed stats from clingo.
          tests (bool or tuple): If True, concretize test dependencies for all packages.
            If a tuple of package names, concretize test dependencies for named
            packages (defaults to False: do not concretize test dependencies).
          setup_only (bool): if True, stop after setup and don't solve (default False).
          allow_deprecated (bool): allow deprecated version in the solve
        """
        specs = [s.lookup_hash() for s in specs]
        reusable_specs = self._check_input_and_extract_concrete_specs(specs)
        reusable_specs.extend(self.selector.reusable_specs(specs))
        setup = SpackSolverSetup(tests=tests)
        output = OutputConfiguration(timers=timers, stats=stats, out=out, setup_only=setup_only)

        CONC_CACHE.flush_manifest()
        CONC_CACHE.cleanup()
        return self.driver.solve(
            setup, specs, reuse=reusable_specs, output=output, allow_deprecated=allow_deprecated
        )

    def solve(self, specs, **kwargs):
        """
        Convenience function for concretizing a set of specs and ignoring timing
        and statistics. Uses the same kwargs as solve_with_stats.
        """
        # Check upfront that the variants are admissible
        result, _, _ = self.solve_with_stats(specs, **kwargs)
        return result

    def solve_in_rounds(
        self, specs, out=None, timers=False, stats=False, tests=False, allow_deprecated=False
    ):
        """Solve for a stable model of specs in multiple rounds.

        This relaxes the assumption of solve that everything must be consistent and
        solvable in a single round. Each round tries to maximize the reuse of specs
        from previous rounds.

        The function is a generator that yields the result of each round.

        Arguments:
            specs (list): list of Specs to solve.
            out: Optionally write the generate ASP program to a file-like object.
            timers (bool): print timing if set to True
            stats (bool): print internal statistics if set to True
            tests (bool): add test dependencies to the solve
            allow_deprecated (bool): allow deprecated version in the solve
        """
        specs = [s.lookup_hash() for s in specs]
        reusable_specs = self._check_input_and_extract_concrete_specs(specs)
        reusable_specs.extend(self.selector.reusable_specs(specs))
        setup = SpackSolverSetup(tests=tests)

        # Tell clingo that we don't have to solve all the inputs at once
        setup.concretize_everything = False

        input_specs = specs
        output = OutputConfiguration(timers=timers, stats=stats, out=out, setup_only=False)
        while True:
            result, _, _ = self.driver.solve(
                setup,
                input_specs,
                reuse=reusable_specs,
                output=output,
                allow_deprecated=allow_deprecated,
            )
            yield result

            # If we don't have unsolved specs we are done
            if not result.unsolved_specs:
                break

            if not result.specs:
                # This is also a problem: no specs were solved for, which means we would be in a
                # loop if we tried again
                raise OutputDoesNotSatisfyInputError(result.unsolved_specs)

            input_specs = list(x for (x, y) in result.unsolved_specs)
            for spec in result.specs:
                reusable_specs.extend(spec.traverse())

        CONC_CACHE.flush_manifest()
        CONC_CACHE.cleanup()


class UnsatisfiableSpecError(spack.error.UnsatisfiableSpecError):
    """There was an issue with the spec that was requested (i.e. a user error)."""

    def __init__(self, msg):
        super(spack.error.UnsatisfiableSpecError, self).__init__(msg)
        self.provided = None
        self.required = None
        self.constraint_type = None


class InternalConcretizerError(spack.error.UnsatisfiableSpecError):
    """Errors that indicate a bug in Spack."""

    def __init__(self, msg):
        super(spack.error.UnsatisfiableSpecError, self).__init__(msg)
        self.provided = None
        self.required = None
        self.constraint_type = None


class OutputDoesNotSatisfyInputError(InternalConcretizerError):

    def __init__(
        self, input_to_output: List[Tuple[spack.spec.Spec, Optional[spack.spec.Spec]]]
    ) -> None:
        self.input_to_output = input_to_output
        super().__init__(
            "internal solver error: the solver completed but produced specs"
            " that do not satisfy the request. Please report a bug at "
            f"https://github.com/spack/spack/issues\n\t{Result.format_unsolved(input_to_output)}"
        )


class SolverError(InternalConcretizerError):
    """For cases where the solver is unable to produce a solution.

    Such cases are unexpected because we allow for solutions with errors,
    so for example user specs that are over-constrained should still
    get a solution.
    """

    def __init__(self, provided, conflicts):
        msg = (
            "Spack concretizer internal error. Please submit a bug report and include the "
            "command, environment if applicable and the following error message."
            f"\n    {provided} is unsatisfiable"
        )

        if conflicts:
            msg += ", errors are:" + "".join([f"\n    {conflict}" for conflict in conflicts])

        super().__init__(msg)

        # Add attribute expected of the superclass interface
        self.required = None
        self.constraint_type = None
        self.provided = provided


class InvalidSpliceError(spack.error.SpackError):
    """For cases in which the splice configuration is invalid."""


class NoCompilerFoundError(spack.error.SpackError):
    """Raised when there is no possible compiler"""


class InvalidExternalError(spack.error.SpackError):
    """Raised when there is no possible compiler"""
