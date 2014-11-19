import re
import collections

from . import util
from sqlalchemy import util as sqlautil
from . import compat

_relative_destination = re.compile(r'(?:(.+?)@)?((?:\+|-)\d+)')


class RevisionError(Exception):
    pass


class RangeNotAncestorError(RevisionError):
    def __init__(self, lower, upper):
        self.lower = lower
        self.upper = upper
        super(RangeNotAncestorError, self).__init__(
            "Revision %s is not an ancestor of revision %s" %
            (lower or "base", upper or "base")
        )


class MultipleHeads(RevisionError):
    def __init__(self, heads, argument):
        self.heads = heads
        self.argument = argument
        super(MultipleHeads, self).__init__(
            "Multiple heads are present for given argument '%s'; "
            "%s" % (argument, ", ".join(heads))
        )


class ResolutionError(RevisionError):
    pass


class RevisionMap(object):
    """Maintains a map of :class:`.Revision` objects.

    :class:`.RevisionMap` is used by :class:`.ScriptDirectory` to maintain
    and traverse the collection of :class:`.Script` objects, which are
    themselves instances of :class:`.Revision`.

    """

    def __init__(self, generator):
        """Construct a new :class:`.RevisionMap`.

        :param generator: a zero-arg callable that will generate an iterable
         of :class:`.Revision` instances to be used.   These are typically
         :class:`.Script` subclasses within regular Alembic use.

        """
        self._generator = generator

    @util.memoized_property
    def heads(self):
        """All "head" revisions as strings.

        This is normally a tuple of length one,
        unless unmerged branches are present.

        :return: a tuple of string revision numbers.

        """
        self._revision_map
        return self.heads

    @util.memoized_property
    def bases(self):
        """All "base" revisions as strings.

        These are revisions that have a ``down_revision`` of None,
        or empty tuple.

        :return: a tuple of string revision numbers.

        """
        self._revision_map
        return self.bases

    @util.memoized_property
    def _revision_map(self):
        """memoized attribute, initializes the revision map from the
        initial collection.

        """
        map_ = {}

        heads = sqlautil.OrderedSet()
        self.bases = ()

        has_branch_labels = set()
        for revision in self._generator():

            if revision.revision in map_:
                util.warn("Revision %s is present more than once" %
                          revision.revision)
            map_[revision.revision] = revision
            if revision.branch_labels:
                has_branch_labels.add(revision)
            heads.add(revision.revision)
            if revision.is_base:
                self.bases += (revision.revision, )

        for rev in map_.values():
            for downrev in rev._down_revision_tuple:
                if downrev not in map_:
                    util.warn("Revision %s referenced from %s is not present"
                              % (rev.down_revision, rev))
                down_revision = map_[downrev]
                down_revision.add_nextrev(rev.revision)
                heads.discard(downrev)

        map_[None] = map_[()] = None
        self.heads = tuple(heads)

        for revision in has_branch_labels:
            self._add_branches(revision, map_)
        return map_

    def _add_branches(self, revision, map_):
        if revision.branch_labels:
            for branch_label in revision._orig_branch_labels:
                if branch_label in map_:
                    raise RevisionError(
                        "Branch name '%s' in revision %s already "
                        "used by revision %s" %
                        (branch_label, revision.revision,
                            map_[branch_label].revision)
                    )
                map_[branch_label] = revision
            revision.branch_labels.update(revision.branch_labels)
            for node in self._get_descendant_nodes([revision], map_):
                node.branch_labels.update(revision.branch_labels)

            parent = node
            while parent and \
                    not parent.is_branch_point and not parent.is_merge_point:

                parent.branch_labels.update(revision.branch_labels)
                if parent.down_revision:
                    parent = map_[parent.down_revision]
                else:
                    break

    def add_revision(self, revision, _replace=False):
        """add a single revision to an existing map.

        This method is for single-revision use cases, it's not
        appropriate for fully populating an entire revision map.

        """
        map_ = self._revision_map
        if not _replace and revision.revision in map_:
            util.warn("Revision %s is present more than once" %
                      revision.revision)
        elif _replace and revision.revision not in map_:
            raise Exception("revision %s not in map" % revision.revision)

        map_[revision.revision] = revision
        self._add_branches(revision, map_)
        if revision.is_base:
            self.bases += (revision.revision, )
        for downrev in revision._down_revision_tuple:
            if downrev not in map_:
                util.warn(
                    "Revision %s referenced from %s is not present"
                    % (revision.down_revision, revision)
                )
            map_[downrev].add_nextrev(revision.revision)
        if revision.is_head:
            self.heads = tuple(
                head for head in self.heads
                if head not in
                set(revision._down_revision_tuple).union([revision.revision])
            ) + (revision.revision,)

    def get_current_head(self, branch_label=None):
        """Return the current head revision.

        If the script directory has multiple heads
        due to branching, an error is raised;
        :meth:`.ScriptDirectory.get_heads` should be
        preferred.

        :param branch_label: optional branch name which will limit the
         heads considered to those which include that branch_label.

        :return: a string revision number.

        .. seealso::

            :meth:`.ScriptDirectory.get_heads`

        """
        current_heads = self.heads
        if branch_label:
            current_heads = self.filter_for_lineage(current_heads, branch_label)
        if len(current_heads) > 1:
            raise MultipleHeads(
                current_heads,
                "%s@head" % branch_label if branch_label else "head")

        if current_heads:
            return current_heads[0]
        else:
            return None

    def _get_base_revisions(self, identifier):
        return self.filter_for_lineage(self.bases, identifier)

    def get_revisions(self, id_):
        """Return the :class:`.Revision` instances with the given rev id
        or identifiers.

        May be given a single identifier, a sequence of identifiers, or the
        special symbols "head" or "base".  The result is a tuple of one
        or more identifiers.

        Supports partial identifiers, where the given identifier
        is matched against all identifiers that start with the given
        characters; if there is exactly one match, that determines the
        full revision.

        """
        if isinstance(id_, (list, tuple, set, frozenset)):
            return sum([self.get_revisions(id_elem) for id_elem in id_], ())
        else:
            resolved_id, branch_label = self._resolve_revision_number(id_)
            return tuple(
                self._revision_for_ident(rev_id, branch_label)
                for rev_id in resolved_id)

    def get_revision(self, id_):
        """Return the :class:`.Revision` instance with the given rev id.

        If a symbolic name such as "head" or "base" is given, resolves
        the identifier into the current head or base revision.  If the symbolic
        name refers to multiples, :class:`.MultipleHeads` is raised.

        Supports partial identifiers, where the given identifier
        is matched against all identifiers that start with the given
        characters; if there is exactly one match, that determines the
        full revision.

        """

        resolved_id, branch_label = self._resolve_revision_number(id_)
        if len(resolved_id) > 1:
            raise MultipleHeads(resolved_id, id_)
        elif resolved_id:
            resolved_id = resolved_id[0]

        return self._revision_for_ident(resolved_id, branch_label)

    def _resolve_branch(self, branch_label):
        try:
            branch_rev = self._revision_map[branch_label]
        except KeyError:
            try:
                nonbranch_rev = self._revision_for_ident(branch_label)
            except ResolutionError:
                raise ResolutionError("No such branch: '%s'" % branch_label)
            else:
                return nonbranch_rev
        else:
            return branch_rev

    def _revision_for_ident(self, resolved_id, check_branch=None):
        if check_branch:
            branch_rev = self._resolve_branch(check_branch)
        else:
            branch_rev = None

        try:
            revision = self._revision_map[resolved_id]
        except KeyError:
            # do a partial lookup
            revs = [x for x in self._revision_map
                    if x and x.startswith(resolved_id)]
            if branch_rev:
                revs = self.filter_for_lineage(revs, check_branch)
            if not revs:
                raise ResolutionError(
                    "No such revision or branch '%s'" % resolved_id)
            elif len(revs) > 1:
                raise ResolutionError(
                    "Multiple revisions start "
                    "with '%s': %s..." % (
                        resolved_id,
                        ", ".join("'%s'" % r for r in revs[0:3])
                    ))
            else:
                revision = self._revision_map[revs[0]]

        if check_branch and revision is not None:
            if not self._shares_lineage(
                    revision.revision, branch_rev.revision):
                raise ResolutionError(
                    "Revision %s is not a member of branch '%s'" %
                    (revision.revision, check_branch))
        return revision

    def filter_for_lineage(self, targets, check_against):
        id_, branch_label = self._resolve_revision_number(check_against)

        shares = []
        if branch_label:
            shares.append(branch_label)
        if id_:
            shares.append(id_[0])

        #shares = branch_label or (id_[0] if id_ else None)

        return [
            tg for tg in targets
            if self._shares_lineage(tg, shares)]

    def _shares_lineage(self, target, test_against_revs):
        if not test_against_revs:
            return True
        if not isinstance(target, Revision):
            target = self._revision_for_ident(target)

        test_against_revs = [
            self._revision_for_ident(test_against_rev)
            if not isinstance(test_against_rev, Revision)
            else test_against_rev
            for test_against_rev
            in util.to_tuple(test_against_revs, default=())
        ]

        return bool(
            set(self._get_descendant_nodes([target]))
            .union(self._get_ancestor_nodes([target]))
            .intersection(test_against_revs)
        )

    def _resolve_revision_number(self, id_):
        if isinstance(id_, compat.string_types) and "@" in id_:
            branch_label, id_ = id_.split('@', 1)
        else:
            branch_label = None

        # ensure map is loaded
        self._revision_map
        if id_ == 'heads':
            if branch_label:
                return self.filter_for_lineage(
                    self.heads, branch_label), branch_label
            else:
                return self.heads, branch_label
        elif id_ == 'head':
            return (self.get_current_head(branch_label), ), branch_label
        elif id_ == 'base' or id_ is None:
            return (), branch_label
        else:
            return util.to_tuple(id_, default=None), branch_label

    def iterate_revisions(
            self, upper, lower, implicit_base=False, inclusive=False):
        """Iterate through script revisions, starting at the given
        upper revision identifier and ending at the lower.

        The traversal uses strictly the `down_revision`
        marker inside each migration script, so
        it is a requirement that upper >= lower,
        else you'll get nothing back.

        The iterator yields :class:`.Revision` objects.

        """

        if isinstance(upper, compat.string_types) and \
                _relative_destination.match(upper):

            reldelta = 1 if inclusive else 0
            match = _relative_destination.match(upper)
            relative = int(match.group(2))
            branch_label = match.group(1)
            if branch_label:
                from_ = "%s@head" % branch_label
            else:
                from_ = "head"
            revs = list(
                self._iterate_revisions(
                    from_, lower,
                    inclusive=inclusive, implicit_base=implicit_base))
            revs = revs[-relative - reldelta:]
            if len(revs) != abs(relative) + reldelta:
                raise RevisionError(
                    "Relative revision %s didn't "
                    "produce %d migrations" % (upper, abs(relative)))
            return iter(revs)
        elif isinstance(lower, compat.string_types) and \
                _relative_destination.match(lower):
            reldelta = 1 if inclusive else 0
            match = _relative_destination.match(lower)
            relative = int(match.group(2))
            branch_label = match.group(1)

            if branch_label:
                to_ = "%s@base" % branch_label
            else:
                to_ = "base"

            revs = list(
                self._iterate_revisions(
                    upper, to_,
                    inclusive=inclusive, implicit_base=implicit_base))
            revs = revs[0:-relative + reldelta]
            if len(revs) != abs(relative) + reldelta:
                raise RevisionError(
                    "Relative revision %s didn't "
                    "produce %d migrations" % (lower, abs(relative)))
            return iter(revs)
        else:
            return self._iterate_revisions(
                upper, lower, inclusive=inclusive, implicit_base=implicit_base)

    def _get_descendant_nodes(self, targets, map_=None, check=False):
        return self._iterate_related_revisions(
            lambda rev: rev.nextrev,
            targets, map_=map_, check=check
        )

    def _get_ancestor_nodes(self, targets, map_=None, check=False):
        return self._iterate_related_revisions(
            lambda rev: rev._down_revision_tuple,
            targets, map_=map_, check=check
        )

    def _iterate_related_revisions(self, fn, targets, map_, check=False):
        if map_ is None:
            map_ = self._revision_map

        todo = collections.deque()
        for target in targets:
            todo.append(target)
            if check:
                per_target = set()
            while todo:
                rev = todo.pop()
                todo.extend(
                    map_[rev_id] for rev_id in fn(rev))
                if check:
                    per_target.add(rev)
                yield rev
            if check and per_target.intersection(targets).difference([target]):
                raise RevisionError(
                    "Requested revision %s overlaps with "
                    "other requested revisions" % target.revision)

    def _iterate_revisions(
            self, upper, lower, inclusive=True, implicit_base=False):
        """iterate revisions from upper to lower.

        The traversal is depth-first within branches, and breadth-first
        across branches as a whole.

        """

        requested_lowers = self.get_revisions(lower)

        # some complexity to accommodate an iteration where some
        # branches are starting from nothing, and others are starting
        # from a given point.  Additionally, if the bottom branch
        # is specified using a branch identifier, then we limit operations
        # to just that branch.

        limit_to_lower_branch = \
            isinstance(lower, compat.string_types) and lower.endswith('@base')

        uppers = self.get_revisions(upper)
        upper_ancestors = set(self._get_ancestor_nodes(uppers, check=True))

        if limit_to_lower_branch:
            lowers = self.get_revisions(self._get_base_revisions(lower))
        elif implicit_base and requested_lowers:
            lower_ancestors = set(
                self._get_ancestor_nodes(requested_lowers)
            )
            lower_descendants = set(
                self._get_descendant_nodes(requested_lowers)
            )
            base_lowers = set()
            candidate_lowers = upper_ancestors.\
                difference(lower_ancestors).\
                difference(lower_descendants)
            for rev in candidate_lowers:
                for downrev in rev._down_revision_tuple:
                    if self._revision_map[downrev] in candidate_lowers:
                        break
                else:
                    base_lowers.add(rev)
            lowers = base_lowers.union(requested_lowers)
        elif implicit_base:
            base_lowers = set(self.get_revisions(self.bases))
            lowers = base_lowers.union(requested_lowers)
        elif not requested_lowers:
            lowers = set(self.get_revisions(self.bases))
        else:
            lowers = requested_lowers

        total_space = set(
            rev.revision for rev in upper_ancestors).intersection(
            rev.revision for rev
            in self._get_descendant_nodes(lowers, check=True)
        )

        if not total_space:
            raise RangeNotAncestorError(lower, upper)

        branch_endpoints = set(
            rev.revision for rev in
            (self._revision_map[rev] for rev in total_space)
            if rev.is_branch_point and
            len(total_space.intersection(rev.nextrev)) > 1
        )

        todo = collections.deque(
            r for r in uppers if r.revision in total_space)
        stop = set(lowers)
        if implicit_base:
            stop.update(self._get_ancestor_nodes(requested_lowers))
        while todo:
            stop.update(
                rev.revision for rev in todo
                if rev.revision in branch_endpoints)
            rev = todo.popleft()

            # do depth first for elements within the branches
            todo.extendleft([
                self._revision_map[downrev]
                for downrev in reversed(rev._down_revision_tuple)
                if downrev not in branch_endpoints and downrev not in stop
                and downrev in total_space])

            # then put the actual branch points at the end of the
            # list for subsequent traversal
            todo.extend([
                self._revision_map[downrev]
                for downrev in rev._down_revision_tuple
                if downrev in branch_endpoints and downrev not in stop
                and downrev in total_space
            ])

            if not inclusive and rev in requested_lowers:
                continue
            yield rev


class Revision(object):
    """Base class for revisioned objects.

    The :class:`.Revision` class is the base of the more public-facing
    :class:`.Script` object, which represents a migration script.
    The mechanics of revision management and traversal are encapsulated
    within :class:`.Revision`, while :class:`.Script` applies this logic
    to Python files in a version directory.

    """
    nextrev = frozenset()

    revision = None
    """The string revision number."""

    down_revision = None
    """The ``down_revision`` identifier(s) within the migration script."""

    branch_labels = None
    """Optional string/tuple of symbolic names to apply to this
    revision's branch"""

    def __init__(self, revision, down_revision, branch_labels=None):
        self.revision = revision
        self.down_revision = tuple_rev_as_scalar(down_revision)
        self._orig_branch_labels = util.to_tuple(branch_labels, default=())
        self.branch_labels = set(self._orig_branch_labels)

    def add_nextrev(self, rev):
        self.nextrev = self.nextrev.union([rev])

    @property
    def _down_revision_tuple(self):
        return util.to_tuple(self.down_revision, default=())

    @property
    def is_head(self):
        """Return True if this :class:`.Revision` is a 'head' revision.

        This is determined based on whether any other :class:`.Script`
        within the :class:`.ScriptDirectory` refers to this
        :class:`.Script`.   Multiple heads can be present.

        """
        return not bool(self.nextrev)

    @property
    def is_base(self):
        """Return True if this :class:`.Revision` is a 'base' revision."""

        return self.down_revision is None

    @property
    def is_branch_point(self):
        """Return True if this :class:`.Script` is a branch point.

        A branchpoint is defined as a :class:`.Script` which is referred
        to by more than one succeeding :class:`.Script`, that is more
        than one :class:`.Script` has a `down_revision` identifier pointing
        here.

        """
        return len(self.nextrev) > 1

    @property
    def is_merge_point(self):
        """Return True if this :class:`.Script` is a merge point."""

        return len(self._down_revision_tuple) > 1


def tuple_rev_as_scalar(rev):
    if not rev:
        return None
    elif len(rev) == 1:
        return rev[0]
    else:
        return rev
