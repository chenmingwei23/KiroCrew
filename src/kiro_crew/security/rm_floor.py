"""Argv-structural floor for recursive-force ``rm`` deletion of root / home.

Split out of ``argv_floor.py`` as a cohesive sibling: this module owns the
recursive-force ``rm`` deny floor and nothing else. ``argv_floor.is_denied``'s
caller reaches it through :func:`_recursive_force_rm_targets` (and the
fail-closed fallback), which read only the ``rm`` command's OWN argv and return
the catastrophic target set ``{"root", "home"}`` a command deletes.

Enforcement lives here rather than in the regex tier: a whole-line text pattern
cannot tell an ``rm`` operand from the same text quoted in a ``git commit -m``
message or a ``grep`` pattern, so the two ``rm`` catalog patterns are stripped
from the regex tier in ``is_denied`` and this floor is their sole enforcement.
"""

from __future__ import annotations

import fnmatch as _fnmatch
import os as _os
import posixpath as _posixpath
import re

from . import shell_normalizer as _shell_normalizer
from .shell_normalizer import (
    _DATA_CONSUMER_PROGRAMS,
    _argv_programs,
    _data_consumer_exempt,
    _decode_shell_quoted_literals,
    _ends_argv,
    _is_stdin_code_interpreter,
    _program_basename,
    _shell_payload_walk,
    _split_shell_words,
    _stdin_program_text,
    _substitution_depth_delta,
)

# ── Recursive-force ``rm`` deletion floor ──
# ``rm`` recursively force-deleting the filesystem ROOT or the user's HOME is
# catastrophic; a path UNDER either (``/tmp/scratch``, ``$HOME/.cache``) is an
# ordinary cleanup and must stay allowed. The catalog literals ``rm -rf /`` /
# ``rm -rf ~`` only matched one flag spelling, and every attempt to widen them
# as a REGEX went wrong two ways at once:
#   * a left-to-right pattern cannot see flags AFTER the operand, which GNU
#     ``getopt`` accepts (``rm / -rf --no-preserve-root``); and
#   * a text pattern matches a SUBSTRING of the whole command line, so it fired
#     on ``/tmp/x`` (a descendant of ``/``) and on the words ``rm -fr /`` sitting
#     inside a ``git commit -m`` message or a ``grep`` pattern.
# The only sound closure is argv-STRUCTURAL and EXACT, like the self-protection
# and git-publish floors: tokenize, look only at the ``rm`` command's OWN argv,
# collect the flags from every position, and deny only when a resolved operand
# IS the root or the home directory itself — never a descendant, never a text
# mention. This floor is therefore the SOLE enforcement (its catalog patterns are
# stripped from the regex tier in ``is_denied``, exactly as git-publish is), so
# there is no whole-line text match left to fire on a commit message.
#
# The tokens come from ``_split_shell_words`` — the RAW, quote-resolved but
# ENV-UNEXPANDED split — for two reasons the review named: (1) a home operand
# must be classified by its written spelling (``~`` / ``$HOME`` / ``${HOME}``),
# because the expanding tokenizer turns ``$HOME`` into its absolute path which
# then reads as ROOT, inverting the home rule's opt-out (GPT + Opus finding); and
# (2) it keeps the classification on what the argv literally is.
#
# The floor fires only for a command whose PROGRAM is ``rm`` (``_argv_programs``
# tracks command boundaries), so ``confirm -rf /``, an ``rm`` mentioned as data
# (``echo rm -rf /``), and a sibling command's flags (``ls -rf; rm /tmp/x``) do
# not trigger it.


#: ``rm``'s long options, so an abbreviation can be tested for ambiguity. GNU
#: ``getopt_long`` accepts any UNAMBIGUOUS prefix of a long option, so ``rm
#: --rec …`` and ``rm --for …`` run the identical recursive/force delete while a
#: fixed ``--recursive``/``--force`` string comparison would miss them (GPT
#: security-class). A prefix is honoured only when it matches exactly ONE
#: of ``rm``'s long options — ``--r`` resolves to ``--recursive`` (nothing else
#: begins with ``r``), ``--f`` to ``--force`` — never a prefix shared by two.
_RM_LONG_OPTIONS: tuple[str, ...] = (
    "--recursive",
    "--force",
    "--dir",
    "--interactive",
    "--no-preserve-root",
    "--one-file-system",
    "--preserve-root",
    "--verbose",
    "--help",
    "--version",
)


def _rm_long_option_resolves_to(tok: str, target: str) -> bool:
    """Whether *tok* is an unambiguous long-option abbreviation of *target*.

    *tok* must be ``--`` followed by a NON-EMPTY prefix (``--`` alone is the
    end-of-options marker, handled elsewhere), and among ``rm``'s long options
    exactly one must start with that prefix, and it must be *target*. An exact
    spelling is trivially unambiguous. GNU stops at the first ``=`` (``--rec=…``),
    so the option name is taken up to it.
    """
    if not tok.startswith("--") or tok == "--":
        return False
    name = tok[: tok.index("=")] if "=" in tok else tok
    matches = [opt for opt in _RM_LONG_OPTIONS if opt.startswith(name)]
    return matches == [target] or (target in matches and name == target)


#: Whether an ``rm`` argument token carries the recursive flag: the long option
#: ``--recursive`` (or an unambiguous prefix of it), or a single-dash short
#: cluster containing ``r`` (``-r`` / ``-rf`` / ``-fr`` / ``-rfv`` …). A ``--``
#: long option is never read as a short cluster, so ``--force`` is not recursive.
def _rm_is_recursive_flag(tok: str) -> bool:
    if tok.startswith("--"):
        return _rm_long_option_resolves_to(tok, "--recursive")
    return bool(re.fullmatch(r"-[a-z]*r[a-z]*", tok))


#: Whether an ``rm`` argument token carries the force flag (``--force`` or an
#: unambiguous prefix of it, or a single-dash short cluster containing ``f``).
def _rm_is_force_flag(tok: str) -> bool:
    if tok.startswith("--"):
        return _rm_long_option_resolves_to(tok, "--force")
    return bool(re.fullmatch(r"-[a-z]*f[a-z]*", tok))


#: The filesystem ROOT ITSELF — ``/`` (a run of slashes) or the ``/*`` glob over
#: its children, with an optional trailing slash, and NOTHING under it. Used for
#: an ``rm`` reached through an EXEC WRAPPER (``setsid rm -rf /``, ``sudo …``):
#: base ``main`` caught a wrapper-reached descendant only incidentally, as a
#: substring of its ``rm -rf /`` literal, so denying wrapper-reached DESCENDANTS
#: newly refuses benign work the widening never intended (``docker exec kc-ci
#: rm -fr /tmp/build-cache`` — a container cache cleanup base ALLOWED, since its
#: literal is ``rm -rf`` not ``rm -fr``; Security Scope ruling). So a
#: wrapper-reached ``rm`` denies only the catastrophic root ITSELF.
_RM_ROOT_ITSELF_RE = re.compile(r"/+(?:\*/*)?")
#: The HOME dir ITSELF — ``~`` / ``$HOME`` / ``${HOME}``, bare or with a RUN of
#: trailing slashes (``~//`` / ``~///``) or the ``~/*`` glob, nothing under it.
#: A path-collapsing shell treats ``~//`` and ``~///`` as home, so any run of
#: trailing slashes is accepted; the glob ``*`` is
#: admitted only as the whole remainder after the slashes. Wrapper-reached only.
_RM_HOME_ITSELF_RE = re.compile(
    r"(?:~|\$\{home\}|\$home(?![a-z0-9_]))(?:/+(?:\*/*)?)?", re.IGNORECASE
)
#: Escape / quote / substitution characters that can reconstruct the ``rm``
#: program name from text that does not contain the literal ``rm`` (a folded
#: ``"r\<nl>m"``, an octal ``$'r\555'``). The cheap pre-filter admits a command
#: carrying any of these so the walk gets a chance to decode it.
_RM_OBFUSCATION_MACHINERY_RE = re.compile(r"[\\$`'\"]")

#: Ceiling on how many nested-frame descents ONE top-level classification may
#: make. Each ``find -exec`` / ``sh -c`` / interpreter-code span classified as
#: its own argv recurses back into :func:`_rm_targets_in_argv`, so a crafted
#: nest (``sh -c 'sh -c 'sh -c … rm -rf /'''``) would fan out and hang the
#: SYNCHRONOUS PreToolUse gate — measured seconds on a ~120-byte command (Opus
#: security-class). The budget is a single mutable cell threaded through the
#: recursion and decremented on every descent; once it reaches zero no further
#: nested span is opened, so the total work is linear in the cap regardless of
#: nesting depth. It fails SAFE: a real ``rm`` at any reachable depth is already
#: classified by the frames the walk visits BEFORE the cap bites (a genuine
#: nested wipe denies at the shallow frame that carries it), and the raw tier
#: still sees the whole command text — so the cap drops only pathological
#: deep-nest coverage, never a shallow real target. 64 is far past any real
#: command's nesting yet bounds a hostile one to a few milliseconds.
_RM_DESCENT_BUDGET = 64


def _rm_operand_before_boundary(operand: str) -> "tuple[str, bool]":
    """The operand text up to its first unquoted control-operator boundary.

    Returns ``(head, ended)``: *head* is the operand with everything from the
    first ``;`` / ``&`` / ``|`` / newline onward removed, and *ended* is True
    when such a boundary was present. The tokens reaching here have already had
    their quotes resolved (raw split) or normalized away (decoded view), so a
    remaining operator character is unquoted and genuinely separates commands —
    ``rm -rf /;reboot`` tokenizes to the single operand ``/;reboot`` whose real
    target is ``/``. Splitting here classifies that
    ``/`` and stops the argv, so a command glued after the boundary is neither
    read as another rm operand nor able to hide the target before it.
    """
    match = _rm_unescaped_boundary(operand)
    if match is None:
        return operand, False
    return operand[:match], True


def _rm_unescaped_boundary(operand: str) -> "int | None":
    """Index of the first control-operator (``;`` / ``&`` / ``|`` / newline) that
    is NOT backslash-escaped, or ``None``.

    ``_split_shell_words`` preserves a source backslash, so ``rm -rf a\\;b /*``
    reaches here as the single operand ``a\\;b`` whose ``;`` is an ESCAPED literal
    filename character (bash runs ``rm`` on a file named ``a;b``), not a command
    separator — splitting there would end the argv before ``/*`` and fail open
    (Opus security-class). A backslash escapes the next character, so a boundary
    char immediately preceded by an odd run of backslashes is literal and skipped.
    """
    k = 0
    n = len(operand)
    while k < n:
        ch = operand[k]
        if ch == "\\":
            k += 2  # the backslash escapes the next char; neither is a boundary
            continue
        if ch in ";&|\n":
            return k
        k += 1
    return None


def _rm_strip_surrounding_quotes(token: str) -> str:
    """Peel balanced surrounding quote pairs from a raw operand token.

    ``_split_shell_words`` leaves a quoted operand quoted (``"$home"``), so an
    exact operand match needs the wrapper removed. Only a matching leading and
    trailing quote of the same kind is peeled, to a fixed point, so an operand
    that merely CONTAINS a quote is left alone.
    """
    return _rm_strip_surrounding_quotes_reporting(token)[0]


def _rm_strip_surrounding_quotes_reporting(token: str) -> "tuple[str, bool]":
    """``(peeled_token, did_peel)`` — like :func:`_rm_strip_surrounding_quotes`
    but reports whether ANY surrounding quote pair was removed.

    A control-operator character that only becomes bare BECAUSE a quote was
    peeled was QUOTED in the source (``';'`` is a literal filename argument, not
    a command separator), so the operand-boundary split must NOT fire on it:
    ``rm -rf ';' /`` has operands ``;`` and ``/`` and really deletes root, but
    treating the peeled ``;`` as a boundary ends the argv before ``/`` and fails
    open (Opus security-class).
    """
    previous = None
    peeled = False
    while token != previous:
        previous = token
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
            peeled = True
    return token, peeled


def _rm_strip_all_quotes(token: str) -> str:
    """Remove every unescaped shell quote character from an operand.

    A shell removes quoting during word expansion, so ``"$HOME"/`` and
    ``$HOME/`` are the SAME path, as are ``"${HOME}"/x`` and ``${HOME}/x`` and a
    split ``"$HO"ME``. ``_rm_strip_surrounding_quotes`` only peels a BALANCED
    surrounding pair, so a PARTIALLY quoted operand keeps a leading ``"`` that
    defeats the ``~`` / ``$HOME`` anchor of the home/root matchers (GPT
    security-class: ``setsid rm -fr "$HOME"/`` bypassed the enabled home rule).
    This yields the de-quoted spelling the matchers are anchored on; a backslash
    escape keeps the quote it escapes (``\\"`` is a literal quote char in the
    filename, not a quoting delimiter).
    """
    out: list[str] = []
    i = 0
    n = len(token)
    while i < n:
        ch = token[i]
        if ch == "\\" and i + 1 < n:
            out.append(token[i + 1])
            i += 2
            continue
        if ch in "\"'":
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _rm_normalize_dot_segments(operand: str) -> str:
    """Collapse ``.`` / ``..`` path segments in an rm operand, LEXICALLY.

    A shell hands ``rm`` the operand verbatim and the kernel resolves the dot
    segments, so ``/./`` / ``/.`` / ``/tmp/../`` are all the filesystem ROOT and
    ``~/./`` is the home dir — yet the exact-root / exact-home matchers see a
    string that is not ``/`` and miss it (GPT security-class: ``setsid rm -fr
    /./`` bypassed the wrapped-root guard). This resolves the segments the way
    ``os.path.normpath`` does but WITHOUT touching the filesystem (no ``realpath``,
    so a symlink is never followed — the resolved sensitive-path keystone remains
    the layer that resolves), and preserves a leading ``~`` / ``$HOME`` /
    ``${HOME}`` marker plus a trailing glob ``*`` so the home and glob matchers
    still fire on the normalized form.

    Returns the operand unchanged when it carries no dot segment, so the raw
    classification (which keeps ``$HOME``/``~`` spellings) is unaffected.
    """
    if "." not in operand:
        return operand
    # Preserve a leading home marker and a trailing ``*`` glob across normpath,
    # which would otherwise mangle ``~`` or drop the glob.
    prefix = ""
    for marker in ("~", "${home}", "$home"):
        if operand[: len(marker)].lower() == marker:
            prefix = operand[: len(marker)]
            operand = operand[len(marker) :] or "/"
            break
    glob_tail = ""
    if operand.endswith("/*"):
        operand, glob_tail = operand[:-1], "*"
    if prefix:
        # A ``..`` after the home marker must collapse against HOME, not against
        # ``/``: with ``HOME=/home/alice``, ``$HOME/../alice`` IS the home dir, but
        # normalizing only the REMAINDER (``/../alice`` -> ``/alice``) collapses
        # ``..`` against root and leaves ``$home/alice``, which the exact-home
        # matcher misses (GPT security-class, exact home deletion). Resolve against
        # the REAL expanded home (the same ``os.path.expanduser("~")`` the shell
        # normalizer uses for ``$HOME``), normalize the WHOLE joined path, then map
        # the result back to the marker spelling the home matcher recognizes:
        # EXACTLY home -> home ITSELF (restore ``prefix``); still UNDER home ->
        # a home descendant (``prefix`` + the sub-path); ESCAPED above home
        # (``$HOME/../..``) -> the bare normalized absolute path (no marker), which
        # the root/descendant matchers then judge on its own. ``normpath`` is
        # lexical — no ``realpath``, so no symlink is followed. ``expanduser`` is
        # platform-native, so on Windows it returns a BACKSLASH path
        # (``C:\Users\x``); fold the separators to ``/`` first so the ``..``
        # collapse against the forward-slash operand is consistent on every
        # platform (Windows CI: ``$HOME/../<home-base>`` must still resolve to home).
        home_real = _posixpath.normpath(_os.path.expanduser("~").replace("\\", "/"))
        joined = home_real + ("" if operand == "/" else operand)
        try:
            collapsed_full = _posixpath.normpath(joined)
        except (TypeError, ValueError):
            return prefix + operand + glob_tail
        # The home MATCHER admits a glob only after a ``/`` separator
        # (``_RM_HOME_ITSELF_RE`` is ``~(?:/+(?:\*/*)?)?``), so a bare ``~*`` /
        # ``~/foo*`` with the separator dropped would never fullmatch and the wipe
        # would fail open (Opus security-class: ``rm -fr ~/./*`` collapsed to
        # ``~*``). Re-emit the ``/`` with the glob tail.
        glob_suffix = "/" + glob_tail if glob_tail else ""
        if collapsed_full == home_real:
            return prefix + glob_suffix
        if collapsed_full.startswith(home_real + "/"):
            return prefix + collapsed_full[len(home_real) :] + glob_suffix
        return collapsed_full + glob_suffix
    try:
        collapsed = _posixpath.normpath(operand)
    except (TypeError, ValueError):
        return prefix + operand + glob_tail
    if prefix and collapsed == ".":
        collapsed = ""
    # ``~`` is tilde-expanded by the shell ONLY as the first character of a word.
    # ``./~`` is a directory literally named ``~`` in the cwd (the shell does NOT
    # expand it), the canonical safe spelling for removing such a stray entry —
    # base's ``rm -rf ~.*`` literal never matched its leading ``./`` (Security
    # Scope regression). When no home prefix was present on the original operand
    # yet ``normpath`` collapsed a leading ``./`` to leave a bare ``~`` segment,
    # that ``~`` is a literal filename, not home: keep the collapsed form from
    # re-reading as a home target by restoring the dot anchor the shell saw.
    if not prefix and collapsed[:1] == "~":
        collapsed = "./" + collapsed
    return prefix + collapsed + glob_tail


def _rm_walk_frames(text_lower: str, raw_text: "str | None") -> "list[tuple[str, list[str], bool]]":
    """``(source, norm_tokens, repaired)`` frames for the rm floor to classify.

    The first block is ``_shell_payload_walk(text_lower)`` — the ordinary
    lowercased walk. When *raw_text* is supplied AND carries an ANSI-C span, a
    SECOND block is the walk of that text with its ``$'…'`` spans decoded
    (case-preserved) then lowercased, so the width-sensitive ``\\U`` unicode
    escape resolves (``is_denied`` lowercases first, which would turn ``\\U`` into
    ``\\u`` and truncate the read at 4 digits).

    ``repaired`` marks the second block, and the caller uses it to classify a
    repaired frame ONLY through its decoded ``norm_tokens`` — never through a
    quote-stripping raw split. ``_decode_shell_quoted_literals`` re-quotes an
    ANSI-C value with ``shlex.quote`` (``$'\\"/\\"'`` -> ``'"/"'``), so a raw
    split of the repaired source would strip BOTH the added shell quotes and the
    LITERAL quotes the decode produced, reading the filename ``"/"`` as the root.
    The original-text walk (always included) is where a raw ``$HOME`` / ``~`` home
    operand is classified, so the repaired block loses no coverage by skipping it.
    Deduplicated: the ordinary command (no ``$'…'``, or the decode changes
    nothing) yields only the first block.
    """
    frames: "list[tuple[str, list[str], bool]]" = [
        (source, toks, False) for source, toks in _shell_payload_walk(text_lower)
    ]
    if raw_text is not None and "$'" in raw_text:
        repaired = _decode_shell_quoted_literals(raw_text).lower()
        if repaired != text_lower:
            frames.extend((source, toks, True) for source, toks in _shell_payload_walk(repaired))
    return frames


def _recursive_force_rm_targets(
    text_lower: str, *, raw_text: "str | None" = None
) -> "frozenset[str]":
    """Which catastrophic target(s) a top-level ``rm`` recursively force-deletes.

    Returns a subset of ``{"root", "home"}`` — ``root`` when a resolved operand
    IS the filesystem root, ``home`` when one IS the home directory (by ``~`` or
    the ``$HOME`` variable). Empty when the command is not a recursive-force
    ``rm`` against such an EXACT target; a descendant (``/tmp/x``,
    ``$HOME/.cache``) and a mere text mention both return empty.

    ``--no-preserve-root`` is a trigger on its own (meaningless without ``-rf``,
    and its whole purpose is to defeat the ``/`` guard); otherwise BOTH a
    recursive and a force flag must be present, in any position. A ``--``
    end-of-options marker stops flag parsing, so a token after it is an operand
    even if it is dash-shaped — matching GNU ``rm``.

    Every command FRAME is inspected — the top-level argv and the argv of every
    nested shell payload (``bash -c '…'``, ``sh -c``, ``$(…)``, a here-string, a
    chained segment). Each frame is re-split from its RAW source with
    ``_split_shell_words`` (quote-resolved but ENV-UNEXPANDED), so ``$HOME`` is
    classified by its written form rather than the home path a shlex expansion
    would produce (which would read as root). This is the same payload descent
    the self-protection floor uses, so a wrapper (``sudo rm -rf /``), a nested
    script (``bash -c 'rm -rf /'``) and a chain (``… && rm -rf /``) are all
    reached, while the frame's own ``_argv_programs`` scoping keeps a string that
    is merely an argument to another program (a ``git commit -m`` message, a
    ``grep`` pattern) from ever being read as an ``rm`` command.

    *raw_text* is the ORIGINAL-case command, when the caller has it. Bash's
    ANSI-C unicode escapes are CASE-SENSITIVE in width (``\\u`` is 4 hex digits,
    ``\\U`` is 8), so a ``$'\\U0000002d…'`` spelling decodes correctly only from
    case-preserved text -- the lowercased ``\\u`` truncates at 4 digits and reads
    the wrong character. When *raw_text* is supplied its ANSI-C spans are decoded
    (case-preserved) then lowercased and walked as an ADDITIONAL frame source, so
    the ``\\U`` spelling is caught the same as its ``\\u`` twin.
    """
    # Cheap necessary condition. A plain ``rm`` invocation contains the literal
    # ``rm``; an OBFUSCATED one (``"r\<nl>m"``, ``$'r\555'``) does not — its ``rm``
    # is built by escape/quote/substitution machinery whose decoded output can be
    # any character, so the only sound cheap gate is "contains ``rm`` OR contains
    # such machinery". When neither is present the walk cannot yield an ``rm``.
    if "rm" not in text_lower and not _RM_OBFUSCATION_MACHINERY_RE.search(text_lower):
        return frozenset()
    found: set[str] = set()
    for source, norm_tokens, repaired in _rm_walk_frames(text_lower, raw_text):
        # The DECODED view (payload walk's own tokens) is always classified: it
        # resolves ANSI-C / unicode escapes and env expansion, so ``rm -rf $'/'``
        # / ``$'\u002f'`` is caught as the exact root, and a ``$'"/"'`` filename's
        # LITERAL quotes stay in the token so it is NOT misread as root.
        found |= _rm_targets_in_argv(norm_tokens, strip_quotes=False)
        # Base-literal pin: base denied the contiguous unquoted text ``rm -rf /``
        # / ``rm -rf ~`` wherever it appeared, including behind a data consumer
        # (``echo rm -rf /``). The structural exemption must not walk that back, so
        # the literal is denied here regardless of the exemption — matched on the
        # frame SOURCE text, outside quotes, so a quoted search-verb pattern or a
        # ``git commit -m`` message stays exonerated.
        found |= _rm_base_literal_bare_tokens(source)
        # A REPAIRED frame (from the ANSI-C-decoded copy) is classified ONLY via
        # its decoded tokens above. Its raw source has been through
        # ``_decode_shell_quoted_literals`` + ``shlex.quote``, so a quote-stripping
        # raw split would peel the shell quotes shlex added AND the LITERAL quotes
        # the decode produced (``$'"/"'`` -> ``'"/"'`` -> ``/``), reading a
        # filename as the root. The raw-spelling ``$HOME`` / ``~`` classification
        # it would otherwise add is already covered by the ORIGINAL-text frame.
        if repaired:
            if {"root", "home"} <= found:
                break
            continue
        # Non-repaired frame: also classify the RAW split, which keeps ``$HOME`` /
        # ``~`` unexpanded so home is classified by its written spelling. Surrounding
        # SHELL quotes are stripped only here (``"$HOME"`` -> ``$HOME``).
        found |= _rm_targets_in_argv(_split_shell_words(source), strip_quotes=True)
        # A command-substitution OPERAND resolves to its OUTPUT, which the raw
        # split classifies verbatim (the unresolved ``$(printf /)`` spelling), so
        # ``rm -rf --no-preserve-root "$(printf /)"`` keeps ``$(printf /)`` as the
        # operand and the root wipe is missed (GPT security-class). Resolve each
        # ``$(…)`` / backtick operand to the word it STATICALLY expands to (the
        # same narrow ``echo``/``printf`` resolver the sibling argv floor uses) and
        # re-classify, so the resolved ``/`` reaches the operand matchers. A
        # dynamic generator resolves to a non-matching sentinel, so this can only
        # ADD coverage, never conjure a target.
        resolved_subst = _rm_resolve_substitution_operands(_split_shell_words(source))
        if resolved_subst is not None:
            found |= _rm_targets_in_argv(resolved_subst, strip_quotes=True)
        # Execution-substitution bodies the shared walk does not surface as their
        # own frames: a ``$(…)`` / backtick command substitution nested INSIDE a
        # double-quoted argument (the enclosing quote makes the closing ``)``
        # quote-inactive, so ``_substitution_bodies`` over-reads it), a bash 5.3
        # ``${ …;}`` funsub, and a bare ``(…)`` subshell. Each EXECUTES the command
        # it carries, so an ``rm`` inside one is a real wipe even when the
        # substitution's OUTPUT is then consumed as data (``grep -rn "$(rm -rf /)"
        # test/`` runs the wipe before grep starts). Each extracted body is
        # classified as its own argv — flag order, the exact-operand test and the
        # glob shape all apply inside it.
        for body in _rm_exec_substitution_bodies(source):
            found |= _rm_targets_in_argv(_split_shell_words(body), strip_quotes=True)
        if {"root", "home"} <= found:
            break
    return frozenset(found)


#: The BASE-LITERAL spellings of the two ``rm`` deny rules, as whole-line
#: patterns: base ``main``'s regex was ``rm -rf /.*`` / ``rm -rf ~.*`` — the
#: contiguous text ``rm -rf `` immediately followed by ``/`` or ``~`` and THEN
#: ANY tail (the root/home ITSELF or any DESCENDANT: ``rm -rf /etc``, ``rm -rf
#: /tmp/foo``, ``rm -rf ~/.ssh`` were all denied). They serve two roles:
#:
#: * the FAIL-CLOSED fallback when the structural tokenizer RAISES — the floor
#:   must still deny the one spelling base denied with NO tokenizer (First
#:   Principles items 5+6); and
#: * the base-contiguous pin (:func:`_rm_base_literal_bare_tokens`) that restores
#:   base's DESCENDANT coverage the structural ITSELF matchers deliberately
#:   leave to this text pin — exactly reproducing base's substring match without
#:   the token-adjacency guesswork a structural flag check would need.
#:
#: They recover ONLY base's exact ``rm -rf `` spelling, not the widened flag
#: coverage (``-fr``, split ``-r -f``, long options) — those are the spellings
#: base's literal never contained, so a descendant in a widened spelling (``rm
#: -fr /tmp/x``) is NOT matched here and stays allowed (Security Scope ruling),
#: while the catastrophic root/home ITSELF in a widened spelling is caught by the
#: structural ITSELF matchers regardless. The target is ``/`` / ``~`` immediately
#: after ``rm -rf ``, so ``rm -rf -- /tmp/x`` (``--`` between) and ``rm /tmp/x
#: -rf`` (flags after path) and ``rm -rf $HOME/x`` (``$HOME`` ≠ ``/`` / ``~``) do
#: NOT match — none contained base's contiguous ``rm -rf /`` / ``rm -rf ~`` text.
_RM_ROOT_LITERAL_RE = re.compile(r"rm -rf /\S*")
_RM_HOME_LITERAL_RE = re.compile(r"rm -rf ~\S*")


def _recursive_force_rm_targets_fail_closed(text_lower: str) -> "frozenset[str]":
    """Base-literal ``rm`` targets, for when the structural tokenizer RAISED.

    Applies the pre-widening bare-literal check (``rm -rf /`` / ``rm -rf ~`` as a
    substring of the lowercased command) with NO tokenization, so the floor
    denies the catastrophic literal even when :func:`_recursive_force_rm_targets`
    could not run. This is deliberately the SAME shape ``main``'s deny rule had
    before this change, so the fail path is no weaker than base was.
    """
    found: set[str] = set()
    if _RM_ROOT_LITERAL_RE.search(text_lower):
        found.add("root")
    if _RM_HOME_LITERAL_RE.search(text_lower):
        found.add("home")
    return frozenset(found)


def _rm_base_literal_bare_tokens(source: str) -> "frozenset[str]":
    """Targets of the EXACT base literal ``rm -rf /`` / ``rm -rf ~`` appearing as
    CONTIGUOUS, UNQUOTED text — the one spelling base ``main``'s whole-line regex
    denied that the structural data-consumer exemption must not walk back.

    base denied ANY command whose text contained the contiguous substring
    ``rm -rf /`` / ``rm -rf ~``, including ``echo rm -rf /`` (a mention behind a
    data consumer). The PR narrows that to stop refusing the literal QUOTED inside
    a search-verb pattern or a ``git commit -m`` message — so the pin matches the
    literal only OUTSIDE single/double quotes (First Principles: the exemption
    must not walk back the base pin, but the quoted-mention FP fix stands). It is
    text-contiguous exactly as base's regex was, so a separator between the words
    (``echo rm; -rf /``, ``echo rm`` newline ``-rf /``) is not base's literal and
    is not matched; a glob tail (``rm -rf /*``) is a descendant base matched too.
    """
    found: set[str] = set()
    for pattern, target in ((_RM_ROOT_LITERAL_RE, "root"), (_RM_HOME_LITERAL_RE, "home")):
        for m in pattern.finditer(source):
            if not _index_in_single_quote(source, m.start()) and not _index_in_double_quote(
                source, m.start()
            ):
                found.add(target)
                break
    return frozenset(found)


def _index_in_double_quote(source: str, index: int) -> bool:
    """True if *index* falls inside a double-quoted span of *source*.

    The companion of :func:`_index_in_single_quote`: a double quote that is not
    itself inside a single-quoted span toggles double-quote state. Used by the
    base-literal pin so a ``git commit -m "rm -rf /"`` message stays exonerated
    exactly as the single-quoted form does.
    """
    in_single = False
    in_double = False
    for i, ch in enumerate(source):
        if i >= index:
            break
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
    return in_double


#: A bash 5.3 command funsub: ``${ COMMANDS; }`` / ``${|COMMANDS; }`` — runs the
#: commands in the current shell (unlike ``$(…)``, no subshell). The body runs to
#: the matching ``}``; the leading ``|`` (value-returning form) and a trailing
#: ``;`` are stripped when the body is classified.
_FUNSUB_OPEN_RE = re.compile(r"\$\{[ \t\n|]")


def _index_in_single_quote(source: str, index: int) -> bool:
    """True if *index* falls inside a single-quoted span of *source*.

    A single quote in bash suppresses every expansion, so a ``${`` (or ``$(``,
    backtick, ``(``) inside one is literal text, not a construct. But a single
    quote INSIDE a double-quoted span is itself a literal apostrophe — it opens
    no span — so a naive count of single quotes flips state on an apostrophe in
    ``"it's $(rm -rf /)"`` and wrongly reads the executing ``$(…)`` after it as
    single-quoted. So BOTH quote contexts are
    tracked: a ``'`` toggles single-quote state only when NOT already inside
    double quotes, and a ``"`` toggles double-quote state only when NOT inside
    single quotes. A backslash escape outside single quotes skips the next
    character (in bash a ``\\'`` outside single quotes is a literal apostrophe,
    not a span opener). The result is single-quote state at *index*.
    """
    in_single = False
    in_double = False
    i = 0
    while i < index and i < len(source):
        ch = source[i]
        if ch == "\\" and not in_single:
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        i += 1
    return in_single


def _rm_matching_close(source: str, start: int, opener: str, closer: str) -> int:
    """Index of the *closer* that balances the *opener* already consumed, QUOTE-AWARE.

     Scans from *start* tracking nesting of ``opener``/``closer`` and both quote
     contexts, so an ``opener``/``closer`` INSIDE a single- or double-quoted span
     (or backslash-escaped) does not change the depth — ``$(echo "a)b"; rm -rf /)``
     keeps its real close, where a quote-blind paren count would stop at the ``)``
     inside ``"a)b"`` and truncate the body before the wipe (GPT security-class,
    ). Returns the index of the balancing ``closer``, or ``len(source)``
     when the construct is unterminated (the caller then takes the remainder,
     which only ever feeds the classifier MORE text — the fail-closed direction).
    """
    depth = 1
    j = start
    n = len(source)
    in_single = in_double = False
    while j < n:
        ch = source[j]
        if ch == "\\" and not in_single:
            j += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return j
        j += 1
    return n


def _rm_matching_backtick(source: str, start: int) -> int:
    """Index of the backtick closing the one already consumed, QUOTE-AWARE.

    A backtick inside a SINGLE-quoted span is literal and does not close the
    substitution; inside double quotes a backtick DOES still delimit a command
    substitution, so only single-quote state suppresses it. A backslash escapes
    the next character outside single quotes. Returns the closing backtick's
    index, or ``len(source)`` when unterminated (caller takes the remainder).
    """
    j = start
    n = len(source)
    in_single = in_double = False
    while j < n:
        ch = source[j]
        if ch == "\\" and not in_single:
            j += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "`" and not in_single:
            return j
        j += 1
    return n


def _rm_exec_substitution_bodies(source: str) -> "list[str]":
    """Inner command lines of the execution-substitutions the shared walk misses.

    Three shapes, all of which RUN the command they carry (so an ``rm`` inside is
    executed, not data), and none of which the payload walk surfaces as a frame
    of its own:

    * a ``$(…)`` command substitution or a backtick one nested INSIDE a
      double-quoted word — ``grep -rn "$(rm -rf /)" test/``. The enclosing
      double quote makes the closing ``)`` quote-INACTIVE to the quote-aware
      body scan, so ``_substitution_bodies`` reads past it; but ``$(…)`` executes
      inside double quotes and unquoted, so the body is extracted here.
    * a bash 5.3 ``${ …;}`` funsub — ``grep x ${ rm -rf /;}`` — which the walk
      does not recognise as a substitution at all.
    * a bare ``(…)`` SUBSHELL — ``(rm -rf /)`` — which runs its body in a child
      shell. When it is glued (``(rm``) the tokenizer keeps the ``(`` on the
      program word, so ``rm`` never reaches program position; extracting the
      parenthesised body and classifying it as its own argv recovers it. (The
      spaced form ``( rm -rf / )`` already tokenizes cleanly, so this only ADDS
      the glued spelling.)

    SINGLE-QUOTE AWARE, and that is load-bearing: inside single quotes ``$(``,
    a backtick, ``(`` and ``${`` are all LITERAL — bash executes none of them —
    so ``git commit -m 'see `rm -rf /` warning'`` and ``grep '`rm -rf /`' src/``
    run no ``rm`` and must NOT be extracted (that is exactly the text false
    positive the Security Scope lane rejects). ``$(…)`` and backticks execute
    inside DOUBLE quotes, so they ARE extracted there; a bare ``(…)`` subshell,
    however, is a LITERAL inside double quotes (bash runs no subshell), so a
    parenthesised mention in a double-quoted commit message is NOT extracted
    (Security Scope ruling). Double-quote state is therefore tracked,
    for the bare-``(`` distinction.

    Returned bodies are command lines; the caller classifies each as its own
    argv. Over-extraction (a body that is not really an ``rm``) yields nothing,
    and an unbalanced/unterminated construct yields the remainder, which only ever
    feeds the classifier MORE text — the fail-closed direction.
    """
    bodies: list[str] = []
    n = len(source)
    # ``$(…)`` command substitutions, bare ``(…)`` subshells, and backtick
    # substitutions — skipped when inside a SINGLE-quoted span, where they are
    # literal. A ``(`` preceded by ``$`` is the command-sub opener; any other
    # ``(`` opens a subshell (both matched by the same paren walk).
    #
    # Both quote contexts are tracked: a ``'`` toggles single-quote state only
    # when NOT inside double quotes (an apostrophe in ``"it's $(rm -rf /)"`` is a
    # literal, and must not suppress the executing ``$(…)`` that follows — GPT
    # security-class), and a ``"`` toggles double-quote state only when
    # NOT inside single quotes. A backslash outside single quotes escapes the
    # next character.
    i = 0
    in_single = False
    in_double = False
    while i < n:
        ch = source[i]
        if ch == "\\" and not in_single:
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        if in_single:
            i += 1
            continue
        if ch == "(":
            # A bare ``(`` SUBSHELL inside DOUBLE quotes is a LITERAL character —
            # bash runs no subshell there (unlike ``$(…)`` and backticks, which
            # DO execute in double quotes) — so a parenthesised mention in a
            # double-quoted commit message or log string (``git commit -m "remove
            # the (rm -rf /) step"``) must NOT be extracted (Security Scope ruling
            # the ruling). A ``$(`` command substitution is still extracted in
            # double quotes: it is matched by this same paren walk, and the ``$``
            # just before marks it, so only a bare ``(`` (no leading ``$``) inside
            # double quotes is skipped.
            dollar_sub = i > 0 and source[i - 1] == "$"
            if in_double and not dollar_sub:
                i += 1
                continue
            open_at = i + 1  # body starts just past the '('
            j = _rm_matching_close(source, open_at, "(", ")")
            bodies.append(source[open_at:j] if j < n else source[open_at:])
            i = j + 1
            continue
        if ch == "`":
            j = _rm_matching_backtick(source, i + 1)
            bodies.append(source[i + 1 : j] if j < n else source[i + 1 :])
            i = j + 1 if j < n else n
            continue
        i += 1
    # ``${ …;}`` / ``${|…;}`` funsubs, matched to their closing brace — also only
    # OUTSIDE a single-quoted span (a ``${`` in single quotes is literal).
    for match in _FUNSUB_OPEN_RE.finditer(source):
        if _index_in_single_quote(source, match.start()):
            continue
        depth = 1
        j = match.end()
        while j < n and depth:
            if source[j] == "{":
                depth += 1
            elif source[j] == "}":
                depth -= 1
            j += 1
        body = source[match.end() : j - 1] if depth == 0 else source[match.end() :]
        # Strip the value-returning ``|`` lead and a trailing statement ``;``.
        bodies.append(body.lstrip("|").rstrip().rstrip(";"))
    return bodies


#: A whole-token command substitution: ``$(…)`` or a backtick pair spanning the
#: entire operand word (after surrounding shell quotes are peeled). The output
#: of such a word becomes the operand ``rm`` receives.
_RM_WHOLE_SUBSTITUTION_RE = re.compile(r"\A\$\((?P<body>.*)\)\Z|\A`(?P<btck>.*)`\Z", re.DOTALL)


def _rm_resolve_substitution_operands(tokens: "list[str]") -> "list[str] | None":
    """``tokens`` with each command-substitution OPERAND replaced by its STATIC
    output, or ``None`` when none resolves (so the caller skips a redundant
    re-classification).

    ``rm -rf --no-preserve-root "$(printf /)"`` reaches ``rm`` with the operand
    ``/`` — the substitution's output — but the raw split keeps the unresolved
    ``$(printf /)`` spelling, so the operand matchers never see ``/`` (GPT
    security-class). Resolve a whole-token ``$(…)`` / backtick operand to the word
    it statically expands to, reusing the sibling argv floor's narrow
    ``echo``/``printf`` resolver (literal first operand only). A dynamic generator
    resolves to the ``"\\x00"`` sentinel, which no operand matcher accepts, so
    this can only ADD coverage, never conjure a target. Flag tokens and
    non-substitution operands are left untouched.
    """
    from .argv_floor import _static_substitution_output

    resolved: list[str] = []
    changed = False
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        peeled = _rm_strip_surrounding_quotes(tok)
        m = _RM_WHOLE_SUBSTITUTION_RE.match(peeled)
        if m is not None:
            body = m.group("body")
            if body is None:
                body = m.group("btck") or ""
            output = _static_substitution_output(body)
            if output and output != "\x00":
                resolved.append(output)
                changed = True
                i += 1
                continue
        # An UNQUOTED ``$(…)`` whose body has internal spaces splits across
        # several shlex tokens (``$(echo`` … ``/)``); reassemble from the ``$(``
        # opener to the token closing the balanced ``)`` and resolve the joined
        # body, so ``rm -rf $(echo /)`` resolves its operand too.
        if peeled.startswith("$(") and ")" not in peeled[2:]:
            depth = peeled.count("(") - peeled.count(")")
            j = i + 1
            while j < n and depth > 0:
                depth += tokens[j].count("(") - tokens[j].count(")")
                j += 1
            if depth == 0 and j <= n:
                joined = " ".join(tokens[i:j])
                inner = _rm_strip_surrounding_quotes(joined)
                mm = _RM_WHOLE_SUBSTITUTION_RE.match(inner)
                if mm is not None:
                    body = mm.group("body") or ""
                    output = _static_substitution_output(body)
                    if output and output != "\x00":
                        resolved.append(output)
                        changed = True
                        i = j
                        continue
        resolved.append(tok)
        i += 1
    return resolved if changed else None


#: Multi-call binaries that DISPATCH to the applet named by their first
#: (non-flag) argument: ``busybox rm -rf /`` runs the ``rm`` applet, and
#: ``toybox``/``busybox.exe`` do the same. Here ``rm`` is the dispatcher's first
#: ARGUMENT, not the line's program word and not behind an exec wrapper, so the
#: plain program-position scan and the wrapper set both miss it (GPT
#: security-class). Unlike an exec wrapper, ONLY the first argument is
#: the applet — ``busybox echo rm -rf /`` runs ``echo``, not ``rm`` — so the
#: dispatch is matched positionally, not by wrapper membership.
_RM_APPLET_DISPATCHERS: frozenset[str] = frozenset({"busybox", "toybox"})


def _rm_deescape_unquoted_backslashes(text: str) -> str:
    """Remove backslash escapes as an UNQUOTED inner shell would, so an escaped
    program name reforms.

    A ``bash -c $"\\r\\m -rf /"`` payload reaches the inner shell as the script
    ``\\r\\m -rf /``; unquoted, bash drops each backslash before an ordinary
    character, so ``\\r\\m`` becomes the word ``rm``. The outer walk's
    ``_decode_printf_escapes`` instead maps ``\\r`` to whitespace and drops the
    ``r``, so the ``rm`` never reforms and the wipe was missed (Item 4).

    Backslashes INSIDE single quotes are literal and are left untouched; a
    backslash outside single quotes removes itself and keeps the next character
    (``\\n`` -> ``n``, matching the inner shell's own unquoted lexing rather than
    the C-escape meaning — the shell does not turn an unquoted ``\\n`` into a
    newline). A trailing backslash is dropped.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_single = False
    while i < n:
        ch = text[i]
        if ch == "'":
            in_single = not in_single
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and not in_single and i + 1 < n:
            out.append(text[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


#: ``find``'s two flags that RUN their trailing command span as a real argv
#: (``-execdir`` differs from ``-exec`` only in the working directory), so an
#: ``rm`` inside that span is executed, not data. ``-ok``/``-okdir`` prompt first
#: but still execute, so they are included — the prompt is not a control an agent
#: session can rely on.
_FIND_EXEC_FLAGS: frozenset[str] = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
#: The two tokens that TERMINATE a ``find -exec`` command span: ``;`` (run once
#: per match) and ``+`` (batch).
_FIND_EXEC_TERMINATORS: frozenset[str] = frozenset({";", "+"})
#: ``find`` operators that START the expression proper, ending the path-root
#: list — a grouping/negation token or the ``,`` separator. A leading OPTION
#: (``-L``/``-maxdepth``/``-type``…) does NOT end it: GNU find is lenient about
#: options appearing before the paths, so those are consumed first (see
#: ``_find_search_roots``) rather than hiding the real search root behind them.
_FIND_EXPRESSION_START = frozenset({"(", ")", "!", ","})
#: ``find`` expression predicates that prove the START ROOT is excluded from the
#: ``{}`` match set: a NAME / PATH pattern test. ``find $HOME -exec rm -rf {}``
#: with no such predicate expands ``{}`` to the whole tree (``$HOME`` itself), so
#: the home target stands; a specific NAME / PATH test (``-name __pycache__``)
#: proves ``$HOME`` is not in the set (the home/root dir is not named
#: ``__pycache__`` and its path does not match a specific ``-path`` glob), so that
#: case is a predicate-scoped purge of ordinary developer work and ``{}`` is
#: exempt. A ``-type`` / ``-prune`` / ``-perm`` / ownership / time predicate is
#: NOT here: those can match the start root directory itself, so they do NOT
#: exempt ``{}`` (GPT security-class: ``find "$HOME" -type d`` still wipes home),
#: and a catch-all pattern (``-name '*'``) matches the root too.
_FIND_NAME_PATTERN_PREDICATES: frozenset[str] = frozenset(
    {
        "-name",
        "-iname",
        "-path",
        "-ipath",
        "-wholename",
        "-iwholename",
        "-regex",
        "-iregex",
        "-lname",
        "-ilname",
    }
)
#: A ``-name`` / ``-path`` pattern argument that matches EVERYTHING — a bare glob
#: or regex catch-all — does NOT exclude the start root, so it must not exempt
#: ``{}``. Only a SPECIFIC pattern proves the root is out of the match set.
_FIND_CATCH_ALL_PATTERNS: frozenset[str] = frozenset({"*", "**", ".*", "*/*", "/*"})
#: find operators that make a root-excluding ``-name``/``-path`` predicate NOT
#: gate the ``-exec`` route, so their presence anywhere in the expression voids
#: the ``{}`` exemption (fail closed). Beyond the OR operators: NEGATION
#: (``!`` / ``-not``) INVERTS a predicate, so ``find "$HOME" ! -name __never__
#: -exec rm -rf {}`` runs ``-exec`` on everything that is NOT ``__never__`` —
#: ``$HOME`` itself (GPT security-class, irreversible home wipe) — the opposite
#: of what the positional pattern read suggests; and the COMMA operator (``,``)
#: sequences independent expressions whose later ``-exec`` is not gated by an
#: earlier branch's predicate at all.
_FIND_EXEMPTION_VOIDING_OPERATORS: frozenset[str] = frozenset({"-o", "-or", "!", "-not", ","})
#: find options that CONSUME the following token as their value, so the operand
#: walk must skip BOTH — otherwise ``find -maxdepth 3 "$HOME" -exec …`` would
#: read ``3`` as a search root and miss ``$HOME``. Covers the common
#: value-taking positional-option predicates (``-maxdepth``/``-mindepth``/
#: ``-type``/``-name``/…). An option NOT listed here is assumed flag-only.
#: ``-d``/``-o`` are DELIBERATELY omitted: BSD/macOS ``find -d`` (depth-first)
#: and the ``-o`` OR operator take NO value, so listing them made ``find -d /
#: -exec rm -rf {}`` swallow the root ``/`` as ``-d``'s operand and fail OPEN
#: (Opus security-class); the lowercased global options ``-D``/``-O`` they were
#: meant to cover then miss, which only over-reads a search root — the
#: fail-CLOSED side.
_FIND_OPTIONS_WITH_OPERAND = frozenset(
    {
        "-maxdepth",
        "-mindepth",
        "-type",
        "-xtype",
        "-name",
        "-iname",
        "-path",
        "-ipath",
        "-regex",
        "-iregex",
        "-perm",
        "-user",
        "-group",
        "-uid",
        "-gid",
        "-size",
        "-newer",
        "-mtime",
        "-atime",
        "-ctime",
        "-mmin",
        "-amin",
        "-cmin",
    }
)


def _find_search_roots(tokens: "list[str]", find_at: int) -> "list[str]":
    """The leading path ROOTS of the ``find`` command whose program word is at
    *find_at* — the operands ``{}`` expands to.

    ``find [option …] [root …] [expression]``: GNU find accepts options before
    the paths (``find -maxdepth 3 $HOME …``), so leading OPTIONS are consumed
    first — each ``-flag`` and, when it takes a value (``_FIND_OPTIONS_WITH_OPERAND``),
    its operand — before the path roots are collected. Without that, a leading
    ``-maxdepth 3`` hid ``$HOME`` behind it and ``{}`` resolved to nothing (GPT
    security-class). Collection then runs from the first non-option token
    to the next option / grouping / ``,`` separator. ``find $HOME -exec …`` has
    root ``$HOME``; ``find -maxdepth 3 / -exec …`` has root ``/``; ``find . -exec
    …`` has root ``.`` (relative, not catastrophic).
    """
    n = len(tokens)
    j = find_at + 1
    # Consume leading options (and operand-taking option values).
    while j < n:
        tok = tokens[j]
        if tok.startswith("-") and tok not in _FIND_EXEC_FLAGS:
            if tok in _FIND_OPTIONS_WITH_OPERAND and j + 1 < n:
                j += 2  # skip the option AND its operand
            else:
                j += 1  # flag-only option
            continue
        break
    # Collect the path roots up to the first expression / option token.
    roots: list[str] = []
    while j < n:
        tok = tokens[j]
        if not tok or tok.startswith("-") or tok in _FIND_EXPRESSION_START:
            break
        roots.append(tok)
        j += 1
    return roots


#: ``find`` predicates that test a candidate's WHOLE PATH (``-path``/``-regex``
#: and their variants) versus those that test only its BASENAME (``-name``). A
#: predicate excludes the start root only when its pattern cannot match that
#: root; a whole-path test is checked against the root path, a basename test
#: against the root's last path segment.
_FIND_PATH_PATTERN_PREDICATES: frozenset[str] = frozenset(
    {"-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex", "-lname", "-ilname"}
)
_FIND_REGEX_PATTERN_PREDICATES: frozenset[str] = frozenset({"-regex", "-iregex"})


def _find_predicate_matches_root(predicate: str, pattern: str, root: str) -> bool:
    """True when a ``find`` NAME / PATH *predicate*'s *pattern* could match the
    search *root* itself.

    A predicate excludes the start root only when it provably CANNOT match it
    (GPT security-class: ``find / -path / -exec rm -rf {}`` — ``-path /`` is a
    specific pattern yet matches the start root ``/`` exactly, so the ``{}`` span
    that expands to ``/`` must still be classified). A whole-path predicate
    (``-path``/``-regex``/…) is matched against the root path; a basename
    predicate (``-name``/``-iname``) against the root's last segment. On any
    doubt — an unparseable regex, an unknown predicate — return True (fail
    closed: do NOT treat the predicate as excluding the root).
    """
    root = root.rstrip("/") or "/"
    if predicate in _FIND_REGEX_PATTERN_PREDICATES:
        # ``-regex`` / ``-iregex`` carry an AGENT-AUTHORED pattern. Running
        # ``re.fullmatch`` on it here would evaluate attacker-controlled regex on
        # the SYNCHRONOUS PreToolUse gate, and catastrophic backtracking
        # (``find /aaaa… -regex '/(a+)+b' -exec rm -rf {} \;``) is NOT an
        # ``re.error`` — it just WEDGES the gate instead of returning a verdict
        # (Opus security-class). So never run the pattern: a regex predicate can
        # neither be proven to exclude the root nor cheaply evaluated, so fail
        # closed — treat it as possibly matching the root (``{}`` stays classified).
        return True
    if predicate in _FIND_PATH_PATTERN_PREDICATES:
        candidate = root
    else:  # -name / -iname: basename test
        candidate = _posixpath.basename(root) or root
    icase = predicate.startswith("-i")
    pat = pattern.lower() if icase else pattern
    cand = candidate.lower() if icase else candidate
    return _fnmatch.fnmatchcase(cand, pat)


def _rm_targets_in_find_exec(
    tokens: "list[str]", programs: "list[str]", *, strip_quotes: bool, _budget: "list[int]"
) -> "frozenset[str]":
    """Catastrophic ``rm`` targets inside a ``find … -exec <cmd> … ;/+`` span.

    ``find`` does not read its ``-exec`` argument as data — it runs the span as
    its own argv once per match (``-exec rm -rf {} ;``), so a rooted delete there
    is a real wipe the plain program-position scan cannot see (``rm`` is not the
    line's program, ``find`` is, and ``find`` is not an exec wrapper). GPT
    security-class finding: ``find . -exec rm -rf --no-preserve-root /``
    passed the floor because the exec span was never parsed.

    The ``{}`` placeholder is the CRUX of the home-deletion guard: ``find $HOME
    -exec rm -rf {} ;`` names no rooted operand in the span, yet ``{}`` expands to
    every match under ``$HOME`` and the run wipes the home tree (GPT
    security-class). So ``{}`` is not discarded — it is classified against
    the find command's SEARCH ROOTS (``$HOME`` here), the operands it expands to.
    A ``{}`` under a root/home root therefore reads as that catastrophic target,
    while ``{}`` under a relative or ordinary root (``find . -exec …``, ``find
    /tmp/x …`` through a wrapper) does not.

    Only a command whose PROGRAM is ``find`` is inspected (``_argv_programs``
    scopes it). Each ``-exec``/``-execdir``/``-ok``/``-okdir`` span runs from just
    after the flag to its ``;``/``+`` terminator (or the argv end), with each
    ``{}`` replaced by the search roots, and is classified as its own argv by the
    same rule — so flag order/position and the operand tests all apply inside it.
    """
    found: set[str] = set()
    i = 0
    n = len(tokens)
    find_at = -1
    while i < n:
        token = tokens[i]
        # Track the program word of the find command THIS token belongs to, so a
        # ``{}`` in its exec span resolves to that command's search roots. The
        # start of a find command is a ``find`` token that itself LEADS an argv:
        # ``i == 0`` or the previous token ended the previous command (a bare
        # ``;``/``&``/``|``/``&&``/``||`` separator). ``programs[i]`` is only the
        # per-token basename, so two find commands in one line
        # (``find /tmp/x -name a ; find $HOME -exec rm -rf {} \;``) both carry
        # ``programs == "find"``; keying off ``programs[i] != programs[i-1]`` left
        # ``find_at`` pinned to the FIRST find, so ``{}`` resolved to ``/tmp/x``
        # and the first find's ``-name a`` granted the second's span the root
        # exemption — a ``$HOME`` wipe (Opus security-class). Keying off the
        # command start rebinds ``find_at`` to each find command.
        if _program_basename(token) == "find" and _program_basename(programs[i]) == "find":
            if i == 0 or _ends_argv(tokens[i - 1]):
                find_at = i
        # The flag is find's only when find is the command this token belongs to.
        if token in _FIND_EXEC_FLAGS and _program_basename(programs[i]) == "find":
            roots = _find_search_roots(tokens, find_at) if find_at >= 0 else []
            literal_span: list[str] = []
            resolved_span: list[str] = []
            has_placeholder = False
            j = i + 1
            while j < n and tokens[j] not in _FIND_EXEC_TERMINATORS:
                if tokens[j] == "{}":
                    has_placeholder = True
                    resolved_span.extend(roots)  # {} -> the search roots it expands to
                else:
                    literal_span.append(tokens[j])
                    resolved_span.append(tokens[j])
                j += 1
            if literal_span:
                # The LITERAL span (``{}`` removed) is a command argv classified
                # DIRECT: a literal rooted operand (``find . -exec rm -rf /tmp/x``)
                # is a wipe base matched by substring, so it keeps the descendant
                # contract.
                if _budget[0] > 0:
                    _budget[0] -= 1
                    found |= _rm_targets_in_argv(
                        literal_span, strip_quotes=strip_quotes, _budget=_budget
                    )
            if has_placeholder and roots:
                # ``{}`` expands to each match under the search roots, so an
                # ``rm -rf {}`` there can wipe the root tree — the home-deletion
                # guard base could not see (no literal path to substring-match; GPT
                # security-class). It is exempt ONLY when the expression proves the
                # START ROOT itself cannot be a match: a NAME / PATH pattern test
                # with a SPECIFIC pattern (``find $HOME -name __pycache__ -exec rm
                # -rf {}`` deletes only ``__pycache__`` dirs, never ``$HOME``;
                # Security Scope ruling). A ``-type`` / ``-prune`` / ``-perm`` /
                # ownership / time predicate can match the start root dir itself,
                # so it does NOT exempt ``{}`` (``find "$HOME" -type d -exec rm -rf
                # {}`` DOES expand ``{}`` to ``$HOME``; GPT security-class), and a
                # catch-all pattern (``-name '*'``) matches the root too. With no
                # root-excluding predicate, classify the FULL span with ``{}``
                # replaced by the roots against the target: ``find $HOME -exec rm
                # -rf {}`` denies (home itself), ``find /tmp/x -exec rm -rf {}``
                # allows (a descendant root).
                root_excluded = False
                # A disjunction (``-o`` / ``-or``) before ``-exec`` means the
                # ``-exec`` route runs when the OTHER branch is FALSE, so a
                # root-excluding ``-name``/``-path`` predicate does NOT gate it:
                # ``find "$HOME" -name __never__ -o -exec rm -rf {}`` runs ``-exec``
                # on everything that is NOT ``__never__`` — including ``$HOME``
                # itself (GPT security-class, irreversible home wipe). Fail closed:
                # a predicate exempts ``{}`` only when EVERY route reaching
                # ``-exec`` excludes the root, which a disjunction, a negation
                # (``!``/``-not``) or a comma operator all break, so any of them
                # anywhere in the find expression voids the exemption.
                has_disjunction = any(
                    _program_basename(programs[k]) == "find"
                    and _rm_strip_all_quotes(tokens[k]) in _FIND_EXEMPTION_VOIDING_OPERATORS
                    for k in range(find_at + 1, i)
                )
                for k in range(find_at + 1, i):
                    if has_disjunction:
                        break
                    if (
                        _program_basename(programs[k]) == "find"
                        and tokens[k] in _FIND_NAME_PATTERN_PREDICATES
                    ):
                        pattern_arg = tokens[k + 1] if k + 1 < i else ""
                        stripped = _rm_strip_all_quotes(pattern_arg)
                        if not stripped or stripped in _FIND_CATCH_ALL_PATTERNS:
                            continue
                        # A SPECIFIC pattern excludes the start root only when it
                        # matches NONE of the search roots. ``-path /`` is specific
                        # yet matches the start root ``/`` exactly, so it does NOT
                        # exclude it (GPT security-class). Fail closed: if the
                        # pattern could match any root, ``{}`` stays classified.
                        if roots and not any(
                            _find_predicate_matches_root(tokens[k], stripped, r) for r in roots
                        ):
                            root_excluded = True
                            break
                if not root_excluded and _budget[0] > 0:
                    _budget[0] -= 1
                    # The ``{}``-RESOLVED span is synthetic: base saw ``{}``, not a
                    # literal ``/tmp/x``, so base's ``rm -rf /.*`` never matched it.
                    # The find exemption above already decided root-vs-descendant,
                    # so classify ITSELF-only — a resolved descendant root
                    # (``find /tmp/x -exec rm -rf {}``) must stay allowed.
                    found |= _rm_targets_in_argv(
                        resolved_span,
                        strip_quotes=strip_quotes,
                        base_descendant_pin=False,
                        _budget=_budget,
                    )
            i = j + 1  # step past the terminator
            continue
        i += 1
    return frozenset(found)


#: Shell programs whose ``-c`` argument is a command STRING they execute. When a
#: nested payload's escaped quoting defeats the walk's own descent, the walk can
#: still hand this frame a FLATTENED argv (``['sh', '-c', 'rm', '-rf', '/']``);
#: the tokens after ``-c`` are then the executed command, read here as their own
#: argv so the ``rm`` leads its own command instead of sitting behind ``sh``.
_RM_SHELL_C_PROGRAMS: frozenset[str] = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "ash", "busybox"}
)

#: Language interpreters whose ``-c`` / ``-e`` / ``-E`` argument is a PROGRAM
#: string they execute. A shell command the program runs (``os.system("rm -rf
#: /")``, Perl/Ruby ``system("rm -rf /")``, Node ``execSync("rm -rf /")``) sits
#: in a quoted string literal inside that code, so it is invisible to a shell
#: tokenizer — the interpreter, not ``rm``, is the argv program (GPT
#: security-class). The floor extracts each quoted-string literal from
#: the code and classifies it as an ``rm`` argv.
_RM_INTERPRETER_PROGRAMS: frozenset[str] = frozenset(
    {"python", "python2", "python3", "perl", "ruby", "node", "nodejs", "php"}
)
#: A trailing VERSION suffix on an interpreter basename — ``python3.12`` ->
#: ``python3``, ``python2.7`` -> ``python2``, ``perl5.36`` -> ``perl``,
#: ``ruby2.7`` -> ``ruby`` — so a versioned spelling is matched by the same set
#: (GPT 5.6 F1, security-class: ``python3.12 -c '…'`` bypassed the exact-
#: membership check). The ``.NN`` minor/patch tail is stripped first (keeping the
#: major digit so ``python3`` / ``python2`` stay distinct, both in the set); if
#: that still is not a member, the whole trailing digit/dot run is stripped
#: (``perl5`` -> ``perl``) for the interpreters with no digit-suffixed set entry.
_RM_INTERPRETER_VERSION_RE = re.compile(r"(\.\d+)+$")
_RM_INTERPRETER_VERSION_FULL_RE = re.compile(r"[0-9.]+$")


def _rm_is_interpreter(name: str) -> bool:
    """True if *name*'s basename is a code interpreter, version suffix and all."""
    base = _program_basename(name)
    if base in _RM_INTERPRETER_PROGRAMS:
        return True
    if _RM_INTERPRETER_VERSION_RE.sub("", base) in _RM_INTERPRETER_PROGRAMS:
        return True
    return _RM_INTERPRETER_VERSION_FULL_RE.sub("", base) in _RM_INTERPRETER_PROGRAMS


#: The code-string flags those interpreters accept: ``-c`` (python/php), ``-r``
#: (php inline code), ``-e``/``-E`` (perl/ruby/node). The token after one is the
#: program string. ``-E`` is DANGEROUSLY overloaded: for perl/ruby/node it is an
#: inline-code flag, but for PYTHON it is the ``ignore environment`` option that
#: takes NO argument — reading it as a code flag made ``python3 -E -c '<code>'``
#: (a routine clean-environment invocation) consume ``-c`` as ``-E``'s payload and
#: never classify the real code (GPT 5.6 F2, security-class). So resolve the set
#: PER INTERPRETER rather than one flat membership test.
_RM_INTERPRETER_CODE_FLAGS: frozenset[str] = frozenset({"-c", "-e", "-E", "-r"})
#: Interpreters for which ``-E`` is an OPTION (no code payload), not a code flag.
#: Python's ``-E`` ignores ``PYTHON*`` env vars; its ONLY code flag is ``-c`` —
#: it accepts neither ``-e`` nor ``-E`` as inline code. The shell-payload walk
#: LOWERCASES the command, folding ``-E`` to ``-e``, so for a Python interpreter
#: BOTH ``-e`` and ``-E`` must be dropped or the folded ``-e`` is read as perl's
#: code flag and consumes ``-c`` as its payload (GPT 5.6 F2, security-class).
_RM_INTERPRETER_E_IS_OPTION: frozenset[str] = frozenset({"python", "python2", "python3"})


def _rm_code_flags_for(interp_name: str) -> "frozenset[str]":
    """The code-string flags the interpreter *interp_name* actually accepts.

    ``-e``/``-E`` are code flags for perl/ruby/node but NOT for Python (whose only
    code flag is ``-c``; ``-E`` is its ``ignore environment`` option). The walk
    lowercases the command, folding ``-E`` to ``-e``, so for a Python interpreter
    both are dropped: ``python3 -E -c '<code>'`` then reads ``-c`` as the code flag
    and classifies ``<code>`` instead of swallowing ``-c`` as ``-e``'s payload
    (GPT 5.6 F2).
    """
    base = _program_basename(interp_name)
    base = _RM_INTERPRETER_VERSION_RE.sub("", base) or base
    if base in _RM_INTERPRETER_E_IS_OPTION or base.rstrip("0123456789.") == "python":
        return _RM_INTERPRETER_CODE_FLAGS - {"-e", "-E"}
    return _RM_INTERPRETER_CODE_FLAGS


def _rm_interpreter_reads_stdin_code(tokens: "list[str]", i: int, n: int) -> bool:
    """True when the interpreter at *tokens[i]* runs its PROGRAM from stdin.

    An interpreter reads stdin as code only when its own argv (up to the next
    pipeline boundary) names NO program elsewhere: no ``-c``/``-e``/``-E``/``-r``
    code flag (plain or glued) and no bare script-file operand. ``| python`` /
    ``| python -`` read stdin as code; ``| python p.py`` and ``| python -c '…'``
    supply the program elsewhere, so a piped producer is then data, not code. The
    code-flag set is per-interpreter — Python's ``-E`` is an option, not a flag.
    """
    code_flags = _rm_code_flags_for(tokens[i])
    k = i + 1
    while k < n and not _ends_argv(tokens[k]):
        tok = tokens[k]
        if tok in code_flags:
            return False  # ``-c CODE`` etc. — program is that operand, not stdin
        if len(tok) > 2 and tok[:2] in code_flags:
            return False  # glued ``-c'…'``
        if tok == "-" or tok.startswith("-"):
            k += 1
            continue  # ``-`` = explicit stdin; any other flag is not a script
        if _rm_strip_surrounding_quotes(tok):
            return False  # a bare operand is the SCRIPT FILE it runs instead
        k += 1
    return True


#: AWK-family programs whose FIRST positional operand is the program TEXT (there
#: is no ``-c`` flag): ``awk 'BEGIN{system("rm -rf /")}'`` runs ``rm`` through
#: awk's ``system()`` or a pipe-to-command, so the program text is extracted and
#: its quoted literals classified as an ``rm`` argv exactly like a ``-c`` payload
#: (GPT security-class). The program follows any leading ``-F``/``-v var=val`` /
#: ``-f file`` options; a ``-f`` form names a SCRIPT FILE instead of inline text,
#: so there is no inline program to read and the frame is skipped.
_RM_AWK_PROGRAMS: frozenset[str] = frozenset({"awk", "gawk", "mawk", "nawk"})
#: AWK options that take an OPERAND (consumed before the program text): ``-F fs``
#: (field sep), ``-v var=val`` (assignment), ``-f progfile`` (script file).
_RM_AWK_OPERAND_FLAGS: frozenset[str] = frozenset({"-F", "-v", "-f"})
#: A quoted string literal inside interpreter code: a single- or double-quoted
#: run with no embedded quote of the same kind. The shell command an
#: ``os.system`` / ``system`` / ``exec`` call runs is always such a literal.
_RM_CODE_STRING_LITERAL_RE = re.compile(r"""(?:"([^"]*)"|'([^']*)')""")
#: Interpreter-code call names that RUN their string argument as a shell command
#: — the only positions where an extracted ``rm`` literal is actually EXECUTED.
#: ``os.system`` / ``system`` / ``popen`` / ``exec*`` (Python, Perl, Ruby, PHP),
#: ``subprocess.*`` and ``execSync`` / ``spawnSync`` (Node), ``shell_exec`` /
#: ``passthru`` / ``proc_open`` (PHP), ``qx`` and backticks (Perl/Ruby). A quoted
#: literal that is NOT an argument of one of these is DATA — a classifier
#: argument (``is_denied('rm -fr /')``), a log line, a regex — and base ``main``
#: (a contiguous-text scan) did not deny those non-executing spellings, so the
#: extraction must not either (Security Scope ruling). Matched as the identifier
#: run immediately before the opening quote (through ``(``/``[``/whitespace and a
#: Python string prefix ``r``/``b``/``f``), so ``os.system("…")`` qualifies and
#: ``is_denied('…')`` does not.
_RM_CODE_EXEC_SINKS: frozenset[str] = frozenset(
    {
        "system",
        "popen",
        "exec",
        "execl",
        "execle",
        "execlp",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "run",
        "call",
        "popen2",
        "popen3",
        "check_output",
        "check_call",
        "getoutput",
        "getstatusoutput",
        "execsync",
        "spawnsync",
        "spawn",
        "shell_exec",
        "passthru",
        "proc_open",
        "qx",
        "backtick",
    }
)


def _rm_code_literal_is_sink_argument(code: str, literal_start: int) -> bool:
    """True if the literal opening at *literal_start* is a shell-exec sink arg.

    Looks back from the opening quote to the ENCLOSING call: the identifier run
    before it (``os.system("…")``), or — for a list / tuple argument
    (``subprocess.run(['rm','-rf','/'])``) — the sink name before the ``(`` /
    ``[`` the elements sit in. The look-back crosses only call / list syntax
    (``(`` / ``[`` / ``,`` / whitespace / sibling string literals / Python string
    prefixes) and STOPS at a statement boundary (``;`` / newline) or any other
    character, so a bare literal with no sink before it is DATA, not an executed
    command (``is_denied('rm -fr /')`` — a classifier argument base ``main``
    allowed; Security Scope ruling).
    """
    prefix = code[:literal_start]
    # AWK pipe-to-command: ``print | "rm -rf /"`` / ``print |& "cmd"`` RUNS the
    # command string, so a literal immediately after an awk pipe is a sink arg.
    if re.search(r"\|&?\s*\Z", prefix):
        return True
    j = len(prefix) - 1
    while j >= 0:
        ch = prefix[j]
        if ch in " \t([,":
            j -= 1
            continue
        if ch in "rbfRBF" and (j == 0 or prefix[j - 1] in "([, \t"):
            j -= 1  # a Python / JS string prefix on a sibling literal
            continue
        if ch in "'\"":
            quote = ch  # a sibling string literal in the same arg list — skip it
            j -= 1
            while j >= 0 and prefix[j] != quote:
                j -= 1
            j -= 1
            continue
        if ch.isalnum() or ch in "._":
            end = j + 1
            while j >= 0 and (prefix[j].isalnum() or prefix[j] in "._"):
                j -= 1
            name = prefix[j + 1 : end].rsplit(".", 1)[-1].lower()
            return name in _RM_CODE_EXEC_SINKS
        return False  # ``;`` boundary, ``=`` / ``+`` operator, ``}`` block, …
    return False


#: A herestring redirection (``python3 <<<'code'``): the code is fed to the
#: interpreter's stdin, glued to the ``<<<`` token or the token after it.
_RM_HERESTRING_RE = re.compile(r"^<<<(.*)$", re.DOTALL)
#: A heredoc redirection MARKER (``python3 <<'EOF'`` / ``<<-EOF``): the body
#: runs from the token after the marker to the closing DELIMITER word (here
#: ``EOF``), which is what the interpreter reads on stdin.
_RM_HEREDOC_RE = re.compile(r"^<<-?\s*(.*)$", re.DOTALL)

#: Interpreter string-literal escapes an interpreter DECODES before the string
#: reaches ``os.system`` / ``system`` / ``exec``: hex (``\xNN``), unicode
#: (``\uNNNN`` / ``\UNNNNNNNN``), and octal (``\NNN``). The scanner must decode
#: them too, or ``os.system("rm -rf \x24HOME")`` (``\x24`` = ``$``) is classified
#: with the escape intact and the real ``rm -rf $HOME`` is never seen (GPT 5.6 F3,
#: security-class).
_RM_CODE_ESCAPE_RE = re.compile(r"\\(?:x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|[0-7]{1,3})")


def _rm_decode_code_escapes(literal: str) -> str:
    """Decode ``\\xNN`` / ``\\uNNNN`` / ``\\UNNNNNNNN`` / ``\\NNN`` escapes in an
    interpreter string literal to the characters the interpreter runs.

    Only the numeric escapes that reconstruct a target character
    (``\\x24`` -> ``$``, ``\\057`` -> ``/``) are decoded; other backslash
    sequences are left untouched, since the goal is to defeat obfuscation of the
    target, not to fully re-implement any one language's string grammar. Returns
    the literal unchanged when it carries no such escape (the common case), so a
    plain command string is not reparsed.
    """
    if "\\" not in literal:
        return literal

    def _sub(m: "re.Match[str]") -> str:
        esc = m.group(0)
        try:
            if esc[1] in "xuU":
                return chr(int(esc[2:], 16))
            return chr(int(esc[1:], 8))
        except (ValueError, OverflowError):
            return esc

    return _RM_CODE_ESCAPE_RE.sub(_sub, literal)


def _rm_code_sink_call_id(code: str, literal_start: int) -> int:
    """A stable identity for the sink CALL enclosing the literal at
    *literal_start* — the index of the ``(`` / ``[`` the arguments sit in.

    Literals from the SAME call share this id; literals from two independent
    calls (``run(['rm','-rf','.tox']); run(['df','-h','/'])``) get different
    ids, so the list-split join groups them per call and does not fuse one
    call's ``rm -rf`` with another call's ``/`` into a phantom root wipe
    (Security Scope ruling). Returns ``literal_start`` when no enclosing
    ``(``/``[`` bracket is found (each such literal is then its own group).
    """
    j = literal_start - 1
    while j >= 0:
        ch = code[j]
        if ch in "([":
            return j
        if ch in ") ]":
            return literal_start  # closed sibling call — not our bracket
        if ch in " \t,":
            j -= 1
            continue
        if ch in "'\"":
            quote = ch  # skip a sibling string literal in the same arg list
            j -= 1
            while j >= 0 and code[j] != quote:
                j -= 1
            j -= 1
            continue
        if ch.isalnum() or ch in "._rbfRBF":
            j -= 1
            continue
        return literal_start
    return literal_start


def _rm_targets_in_interpreter_code_payload(
    code: str, *, strip_quotes: bool, _budget: "list[int]"
) -> "frozenset[str]":
    """Catastrophic ``rm`` targets inside one interpreter code string.

    Shared by the ``-c``/``-e`` flag path and the stdin/heredoc/herestring path:
    every quoted string LITERAL in the code is a candidate shell command an
    ``os.system`` / ``system`` / ``exec`` call runs, so each is classified as its
    own ``rm`` argv; the per-CALL sink-literal sequence covers an argv SPLIT
    across list elements (``subprocess.run(['rm','-rf','/'])``); and interpreter
    escapes are decoded first so an obfuscated target is seen.
    """
    found: set[str] = set()
    #: Only a literal that is the ARGUMENT of a shell-exec sink is an executed
    #: command; a bare literal elsewhere is data (``is_denied('rm -fr /')`` — a
    #: classifier argument base ``main`` allowed; Security Scope ruling). Grouped
    #: by enclosing CALL so a list-split argv is reassembled per call, never
    #: across two independent calls (Security Scope: ``run(['rm','-rf','.tox']);
    #: run(['df','-h','/'])`` must NOT fuse into ``rm -rf .tox df -h /``).
    sink_literals_by_call: "dict[int, list[str]]" = {}
    for m in _RM_CODE_STRING_LITERAL_RE.finditer(code):
        literal = m.group(1) if m.group(1) is not None else m.group(2)
        if not literal:
            continue
        if not _rm_code_literal_is_sink_argument(code, m.start()):
            continue
        # Decode interpreter escapes (``\x24`` -> ``$``) so an obfuscated target
        # inside the literal is classified as the character the interpreter runs.
        literal = _rm_decode_code_escapes(literal)
        # Collect per enclosing call for the list-split join below — the elements
        # of ``subprocess.run(['rm','-rf','/'])`` are separate literals and only
        # the first contains ``rm``. Single-classify just the ones that carry
        # ``rm`` (a whole ``os.system("rm -rf /")`` command string).
        sink_literals_by_call.setdefault(_rm_code_sink_call_id(code, m.start()), []).append(literal)
        if "rm" not in literal.lower():
            continue
        if _budget[0] <= 0:
            break
        _budget[0] -= 1
        found |= _rm_targets_in_argv(
            _split_shell_words(literal), strip_quotes=strip_quotes, _budget=_budget
        )
    # An argv SPLIT across list elements of ONE sink call
    # (``subprocess.run(['rm','-rf','/'])``) — join the sink-argument literals
    # WITHIN EACH CALL only, so two independent calls never fuse into a phantom
    # command (Security Scope ruling).
    for literals in sink_literals_by_call.values():
        joined = " ".join(literals)
        if "rm" in joined.lower() and _budget[0] > 0:
            _budget[0] -= 1
            found |= _rm_targets_in_argv(
                _split_shell_words(joined),
                strip_quotes=strip_quotes,
                _budget=_budget,
            )
    return frozenset(found)


#: Exec wrappers whose remaining argv is a COMMAND they run: ``env python3``,
#: ``sudo python3``, ``nohup python3``. ``_argv_programs`` attributes a command's
#: tokens to its FIRST word, so under ``… | env python3`` the token ``python3``
#: carries ``programs[i] == "env"`` and the interpreter scan's
#: ``_rm_is_interpreter(programs[i])`` guard failed, skipping the whole scan —
#: ``env python3`` is the canonical clean-env invocation, not an extreme spelling
#: (GPT 5.6 F4, security-class). Resolve through these wrappers to the EFFECTIVE
#: program before inspecting. ``env`` also takes ``VAR=val`` assignments and
#: ``-i``/``-u NAME`` options before the program, which are skipped here.
_RM_EXEC_WRAPPER_PROGRAMS: frozenset[str] = frozenset(
    {
        "env",
        "sudo",
        "nohup",
        "timeout",
        "stdbuf",
        "nice",
        "ionice",
        "setsid",
        "doas",
        "runuser",
        "pkexec",
        "chroot",
    }
)
#: ``env`` options that CONSUME the following token (``env -u NAME python3``).
_RM_ENV_OPERAND_FLAGS: frozenset[str] = frozenset({"-u", "--unset", "-c", "-s", "-p"})


def _rm_interpreter_is_effective_program(tokens: "list[str]", i: int) -> bool:
    """True when the interpreter at *tokens[i]* is the EFFECTIVE program of its
    command, reached only through leading exec wrappers (``env`` / ``sudo`` / …).

    Walks back from ``i`` to the command start (``i == 0`` or the previous token
    ended an argv) and confirms every token before the interpreter is a wrapper
    program, a wrapper option, an operand of a wrapper option, or a ``VAR=val``
    assignment — so ``printf … | env python3`` and ``sudo -n python3`` present
    ``python3`` as the real program (GPT 5.6 F4). A non-wrapper word before it
    (``echo python3``) means the interpreter is an ARGUMENT, not the program.
    """
    start = i
    while start > 0 and not _ends_argv(tokens[start - 1]):
        start -= 1
    k = start
    while k < i:
        tok = tokens[k]
        base = _program_basename(tok)
        if base in _RM_EXEC_WRAPPER_PROGRAMS:
            k += 1
            # Skip this wrapper's own options and any operand-taking option value.
            while k < i and tokens[k].startswith("-"):
                if _program_basename(tok) == "env" and tokens[k] in _RM_ENV_OPERAND_FLAGS:
                    k += 2
                else:
                    k += 1
            continue
        if "=" in tok and not tok.startswith("-"):
            k += 1  # a ``VAR=val`` assignment before the program
            continue
        return False  # a non-wrapper word before the interpreter — it is an arg
    return True


def _rm_targets_in_interpreter_code(
    tokens: "list[str]", programs: "list[str]", *, strip_quotes: bool, _budget: "list[int]"
) -> "frozenset[str]":
    """Catastrophic ``rm`` targets a language-interpreter code payload runs.

    ``python -c 'import os; os.system("rm -rf /")'`` executes ``rm`` through the
    interpreter: ``python`` is the argv program, and the ``rm -rf /`` lives in a
    quoted string INSIDE the code, so the plain scan and the shell-payload walk
    never see it. When a frame's program is one of ``_RM_INTERPRETER_PROGRAMS``
    the code reaches it three ways, all classified via
    :func:`_rm_targets_in_interpreter_code_payload`:

    * a ``-c``/``-e``/``-E`` code FLAG — the token after it is the program string;
    * a HERESTRING (``python3 <<<'code'``) — stdin fed inline, the code glued to
      the ``<<<`` token or the token after it;
    * a HEREDOC (``python3 - <<'EOF'`` … ``EOF`` / ``python3 <<'EOF'`` …) — the
      body from the token after the ``<<`` marker to the closing delimiter word,
      which the interpreter reads on stdin exactly as ``-c`` code.

    Scoped to a frame whose PROGRAM is the interpreter, so a code flag or a
    heredoc feeding data to another command is untouched.

    Code piped in as an UPSTREAM command's OUTPUT
    (``echo 'os.system("rm -rf /")' | python3``) is not a token of the
    interpreter's own frame, so it is not read HERE; the caller's stdin-producer
    branch classifies it instead, feeding the producer's emitted text through
    this same payload classifier via ``_stdin_program_text``.
    """
    found: set[str] = set()
    i = 0
    n = len(tokens)
    # AWK family: the program TEXT is the first positional operand (after any
    # leading ``-F fs`` / ``-v var=val`` / ``-f progfile`` options), and awk runs
    # a command through ``system(...)`` or a pipe-to-command inside it. A ``-f``
    # names a SCRIPT FILE, so there is no inline program to read.
    while i < n:
        if not (
            _program_basename(tokens[i]) in _RM_AWK_PROGRAMS
            and _program_basename(programs[i]) in _RM_AWK_PROGRAMS
        ):
            i += 1
            continue
        j = i + 1
        while j < n:
            tok = tokens[j]
            # A leading option, consumed before the program text. An
            # operand-taking flag also consumes its operand. Checked BEFORE any
            # boundary test, because the program token itself legitimately carries
            # ``|`` / ``;`` inside awk's quoted code (``BEGIN{print | "rm -rf /"}``)
            # and must not be read as a shell boundary.
            if tok == "-f":
                break  # a script file, no inline program
            if tok in _RM_AWK_OPERAND_FLAGS and j + 1 < n:
                j += 2  # skip the operand-taking flag and its operand
                continue
            if tok.startswith("-") and tok != "-":
                j += 1  # a bare / glued flag (``-F:``) with no separate operand
                continue
            if _ends_argv(tok) and not _rm_strip_surrounding_quotes(tok):
                break  # a bare separator token before any program text
            # First non-option token is the program text (its own quotes peeled).
            program = _rm_strip_surrounding_quotes(tok)
            found |= _rm_targets_in_interpreter_code_payload(
                program, strip_quotes=strip_quotes, _budget=_budget
            )
            break
        i += 1
    i = 0
    while i < n:
        # The interpreter runs the code only when it is the EFFECTIVE program of
        # its command: either it is attributed directly (``python3 -c …`` ->
        # ``programs[i] == "python3"``) or it is reached through leading exec
        # wrappers (``env python3`` -> ``programs[i] == "env"``, but ``python3`` is
        # still the real program; GPT 5.6 F4, security-class).
        if not (
            _rm_is_interpreter(tokens[i])
            and (
                _rm_is_interpreter(programs[i])
                or (
                    _program_basename(programs[i]) in _RM_EXEC_WRAPPER_PROGRAMS
                    and _rm_interpreter_is_effective_program(tokens, i)
                )
            )
        ):
            i += 1
            continue
        # STDIN-PRODUCER data flow (GPT security-class): interpreter code can
        # arrive as an UPSTREAM command's stdout piped into a bare stdin-reading
        # interpreter — ``echo 'os.system("rm -rf /")' | python3`` or ``printf …
        # | python`` — where the code is in no token of the interpreter's OWN
        # argv, so the ``-c``/herestring/heredoc scan below never sees it. When
        # this interpreter reads stdin as CODE (a stdin-code interpreter with no
        # code flag and no script-file operand of its own) and a pipe producer
        # feeds it, the producer's emitted text IS the program it runs:
        # ``_stdin_program_text`` yields exactly those producer tokens (the same
        # cross-frame model the self-protection floor uses), and they are
        # classified as interpreter code. A producer piping into a SCRIPT FILE
        # (``| python p.py``) reads stdin as data, not code, so this does not
        # fire (``_rm_interpreter_reads_stdin_code`` requires no script operand).
        if _is_stdin_code_interpreter(tokens[i]) and _rm_interpreter_reads_stdin_code(tokens, i, n):
            producer = "\n".join(
                _rm_strip_surrounding_quotes(t) for t in _stdin_program_text(tokens, i)
            )
            if producer:
                found |= _rm_targets_in_interpreter_code_payload(
                    producer, strip_quotes=strip_quotes, _budget=_budget
                )
        j = i + 1
        code_flags = _rm_code_flags_for(tokens[i])
        while j < n:
            token = tokens[j]
            if token in code_flags and j + 1 < n:
                code = _rm_strip_surrounding_quotes(tokens[j + 1])
                found |= _rm_targets_in_interpreter_code_payload(
                    code, strip_quotes=strip_quotes, _budget=_budget
                )
                break
            # A GLUED code flag — ``-c'os.system(…)'`` / ``-r"…"`` reaches the
            # interpreter as ONE token (``shlex`` strips only the outer quote), so
            # the exact-equality check above never matches it (GPT 5.6 F1,
            # security-class). A token whose first two chars are a code flag and
            # that carries more after it is the flag with its payload glued on.
            if len(token) > 2 and token[:2] in code_flags:
                code = _rm_strip_surrounding_quotes(token[2:])
                found |= _rm_targets_in_interpreter_code_payload(
                    code, strip_quotes=strip_quotes, _budget=_budget
                )
                break
            herestring = _RM_HERESTRING_RE.match(token)
            if herestring is not None:
                # ``<<<'code'`` — the code may be glued to the ``<<<`` token, or
                # (when the shell split there) the next token is the code word.
                inline = _rm_strip_surrounding_quotes(herestring.group(1))
                if not inline and j + 1 < n:
                    inline = _rm_strip_surrounding_quotes(tokens[j + 1])
                found |= _rm_targets_in_interpreter_code_payload(
                    inline, strip_quotes=strip_quotes, _budget=_budget
                )
                break
            if _RM_HEREDOC_RE.match(token) is not None:
                # ``<<'EOF'`` / ``<<-EOF`` — the delimiter is what follows ``<<``
                # in the marker (``EOF``, quotes stripped); the heredoc BODY is the
                # run of tokens from here to that delimiter word, and that body is
                # the interpreter's stdin code.
                delim = _rm_strip_surrounding_quotes(
                    _RM_HEREDOC_RE.match(token).group(1)  # type: ignore[union-attr]
                ).strip()
                body: list[str] = []
                k = j + 1
                while k < n and _rm_strip_surrounding_quotes(tokens[k]).strip() != delim:
                    body.append(tokens[k])
                    k += 1
                found |= _rm_targets_in_interpreter_code_payload(
                    " ".join(body), strip_quotes=strip_quotes, _budget=_budget
                )
                break
            # A plain command boundary ends this interpreter's argv. Checked AFTER
            # the redirection markers above, because a herestring/heredoc token can
            # itself carry a shell separator inside its code (``<<<'a; b'``) and
            # would otherwise be read as a boundary before the code is inspected.
            if _ends_argv(token):
                break
            j += 1
        i += 1
    return frozenset(found)


def _rm_targets_in_shell_c(
    tokens: "list[str]", programs: "list[str]", *, strip_quotes: bool, _budget: "list[int]"
) -> "frozenset[str]":
    """Catastrophic ``rm`` targets in a ``sh -c <cmd>`` argv flattened into a frame.

    The payload walk normally descends ``bash -c '<script>'`` into a frame of its
    own, but a two-level nest with ESCAPED inner quotes
    (``bash -c 'sh -c "rm -rf \\"/\\""'``) can defeat the inner extraction and
    leave the ``sh -c`` frame's argv flattened to ``['sh', '-c', 'rm', '-rf',
    '/']``. There ``rm`` is not at program position (``sh`` is) and ``sh`` is not
    an exec wrapper, so the plain scan misses it. When a nested-shell program is
    followed by a ``-c`` flag, the tokens after ``-c`` are the command string it
    runs, so they are classified as their own argv — the same treatment
    ``find -exec`` gets. Scoped to a frame whose PROGRAM is the shell
    (``_argv_programs``), so a ``-c`` that is data to another command is untouched.
    """
    found: set[str] = set()
    i = 0
    n = len(tokens)
    while i < n:
        if (
            _program_basename(tokens[i]) in _RM_SHELL_C_PROGRAMS
            and _program_basename(programs[i]) in _RM_SHELL_C_PROGRAMS
        ):
            # Find this shell command's own ``-c`` (before its argv ends), then
            # read the rest of the argv as the command string it executes.
            j = i + 1
            while j < n and not _ends_argv(tokens[j]):
                if tokens[j] == "-c" and j + 1 < n:
                    span = []
                    k = j + 1
                    while k < n and not _ends_argv(tokens[k]):
                        span.append(tokens[k])
                        k += 1
                    if span:
                        # The ``-c`` argument is a command STRING the inner shell
                        # re-parses, so re-split it — its OWN backslash de-escaping
                        # runs there. ``bash -c $"\r\m -rf /"`` reaches the inner
                        # shell as the script ``\r\m -rf /``, whose ``\r\m`` the
                        # inner bash de-escapes to ``rm``; the outer walk's
                        # printf-escape pass had mangled ``\r`` to whitespace and
                        # dropped the ``r``. De-escaping the joined payload the way
                        # the unquoted inner shell does, then splitting, recovers
                        # the ``rm`` program word (Item 4); classify that ONE view.
                        #
                        # Only ONE descent per ``-c`` span: de-escaping a payload
                        # with no backslash escapes is the identity, so the
                        # de-escaped split already covers the plain span — a second
                        # ``_rm_targets_in_argv(span)`` classified the same tokens
                        # again and let one nesting level fan out TWICE, which is
                        # what made a chain of ``sh -c`` spans exponential and hung
                        # the synchronous gate (Opus security-class).
                        if _budget[0] > 0:
                            _budget[0] -= 1
                            payload = _rm_deescape_unquoted_backslashes(" ".join(span))
                            found |= _rm_targets_in_argv(
                                _split_shell_words(payload),
                                strip_quotes=strip_quotes,
                                _budget=_budget,
                            )
                    break
                j += 1
        i += 1
    return frozenset(found)


def _rm_targets_in_argv(
    tokens: "list[str]",
    *,
    strip_quotes: bool,
    base_descendant_pin: bool = True,
    _budget: "list[int] | None" = None,
) -> "frozenset[str]":
    """The catastrophic ``rm`` targets deleted within ONE frame's raw argv.

    Fires for each token whose basename is ``rm`` and that is EXECUTED — ``rm``
    at program position (its command's leading word), the first argument of a
    multi-call dispatcher (``busybox rm``), or ``rm`` whose parent command does
    NOT treat its arguments as data. That last test is a DENYLIST: an ``rm``
    behind ANY parent is executed UNLESS the parent is in
    ``_DATA_CONSUMER_PROGRAMS`` (``echo`` prints, ``cat`` reads, ``cp``/``mv``
    move paths), so an unknown exec wrapper — ``setsid``/``nohup``/``chrt``/… —
    is treated as executable rather than slipping a fixed allowlist. ``rm`` is
    itself a data-consumer program, but that never mis-fires here because a
    program-position ``rm`` is caught by the leading-word test first and never
    reaches the parent check. An ``rm`` that is an argument of a data consumer
    (``echo rm -rf /`` prints, it does not run ``rm``) is skipped.

    From each executed ``rm`` its OWN argv is read forward until the command
    ends, so a sibling command's flags never leak in. A resolved operand is
    denied here when it is the root (``/``, a run of slashes, the ``/*`` glob) or
    home (``~`` / ``$HOME`` / ``${HOME}`` / their ``/*`` glob) dir ITSELF, in ANY
    flag spelling — the catastrophic root/home wipe. A DESCENDANT (``rm
    -rf /etc``, ``rm -rf ~/.ssh``) is NOT denied by this structural pass; base
    ``main``'s own descendant coverage is reproduced by the frame-text pin
    (``rm -rf /.*`` / ``rm -rf ~.*``, :data:`_RM_ROOT_LITERAL_RE`), so only a
    descendant base's contiguous ``rm -rf `` text matched is denied and a widened
    spelling (``rm -fr /tmp/x``) that base never contained stays allowed
    (Security Scope ruling).

    ``strip_quotes`` peels surrounding SHELL quotes from each operand — True for
    the raw split (``"$HOME"`` -> ``$HOME``), False for the decoded view where a
    surrounding quote is a literal character the decode produced (``$'"/"'`` ->
    ``"/"``, a filename, not the root).
    """
    if not tokens:
        return frozenset()
    # One shared descent budget per top-level classification. The public entry
    # (and every non-recursive caller) passes None, so a fresh cell is created
    # here; the sub-helpers thread the SAME cell into their recursive
    # ``_rm_targets_in_argv`` calls, so nested spans draw down one common budget.
    if _budget is None:
        _budget = [_RM_DESCENT_BUDGET]
    programs = _argv_programs(tokens)
    found: set[str] = set()
    found |= _rm_targets_in_find_exec(tokens, programs, strip_quotes=strip_quotes, _budget=_budget)
    found |= _rm_targets_in_shell_c(tokens, programs, strip_quotes=strip_quotes, _budget=_budget)
    found |= _rm_targets_in_interpreter_code(
        tokens, programs, strip_quotes=strip_quotes, _budget=_budget
    )
    # Computed once per argv (not per ``rm`` token): the pipe-into-shell / trailing
    # operator guards ``_data_consumer_exempt`` consults, whose sweep is quadratic
    # per token. ``None`` until the first ``rm`` needs it.
    disqualified: "bool | None" = None
    expect_program = True
    #: Index of the most recent command's program word, so a dispatcher's FIRST
    #: argument (its applet) can be recognised: ``busybox rm -rf /`` runs ``rm``.
    program_word_at = -1
    for i, token in enumerate(tokens):
        is_program_word = (
            expect_program and bool(token) and not _shell_normalizer.ENV_ASSIGNMENT_RE.match(token)
        )
        starts_command = is_program_word
        if is_program_word:
            expect_program = False
            program_word_at = i
        # A glued ``&`` / ``&&`` ENDS the command (backgrounds or chains it), so
        # the next token starts a NEW command that really runs — ``echo hi& rm
        # -rf /`` is two commands, and the ``rm`` is executed, not echo's data.
        # ``_ends_argv`` catches ``|``/``;`` glued to a token but not ``&``, and a
        # standalone ``&`` token is already covered; this adds the glued-tail case
        # . A ``2>&1`` redirection ends in ``1``, not
        # ``&``, so it is not mistaken for a boundary.
        if _ends_argv(token) or token.endswith("&"):
            expect_program = True
        if _program_basename(token) != "rm":
            continue
        # Executed iff ``rm`` leads its own command, or ``rm`` is the FIRST
        # argument of a multi-call dispatcher (``busybox rm`` runs the rm applet),
        # or its parent command does NOT treat its arguments as data. The last
        # test is a DENYLIST, not an allowlist: an exec wrapper set could only
        # ever name the wrappers someone thought of, and ``setsid``/``nohup``/
        # ``chrt``/``ionice``/… or any future one
        # would slip through. So the default for an UNKNOWN parent is EXECUTABLE,
        # and only a parent in ``_DATA_CONSUMER_PROGRAMS`` (``echo`` prints, ``cat``
        # reads, ``cp``/``mv`` move paths) makes the ``rm`` a data mention.
        # ``_data_consumer_exempt`` also refuses the exemption when the argument
        # pipes into a shell or carries a glued new-program operator, so
        # ``echo rm -rf / | sh`` is still executed.
        dispatched_applet = (
            i == program_word_at + 1
            and program_word_at >= 0
            and _program_basename(tokens[program_word_at]) in _RM_APPLET_DISPATCHERS
        )
        # When the command's program is a multi-call dispatcher, its FIRST
        # argument is the applet that actually runs, so THAT — not ``busybox`` —
        # is the effective parent of a later ``rm``. ``busybox echo rm -rf /``
        # runs ``echo``, which prints ``rm -rf /``: a mention, not a wipe. Resolve
        # the effective parent to the applet before the data-consumer test so the
        # dispatcher itself (never a data consumer) does not make its applet's
        # arguments look executed.
        dispatcher_applet_is_consumer = (
            not dispatched_applet
            and program_word_at >= 0
            and program_word_at + 1 < len(tokens)
            and _program_basename(tokens[program_word_at]) in _RM_APPLET_DISPATCHERS
            and _program_basename(tokens[program_word_at + 1]) in _DATA_CONSUMER_PROGRAMS
        )
        if not (starts_command or dispatched_applet):
            if dispatcher_applet_is_consumer:
                continue
            if disqualified is None:
                disqualified = _shell_normalizer._data_consumer_command_disqualified(tokens)
            if _data_consumer_exempt(i, token, programs, tokens, command_disqualified=disqualified):
                continue
        # STRUCTURAL classification (below) denies the EXACT root/home target
        # ITSELF for ANY ``rm`` — direct, dispatcher applet, or EXEC-WRAPPER
        # reached (``sudo``/``setsid``/``docker exec`` …) — because the
        # catastrophic root/home wipe is denied in every flag spelling. base
        # ``main``'s own DESCENDANT coverage (``rm -rf /etc``, ``sudo
        # rm -rf /etc``, ``rm -rf ~/.ssh``) is reproduced at the FRAME-TEXT level
        # by the base-contiguous-literal pin (``rm -rf /.*`` / ``rm -rf ~.*``, see
        # :data:`_RM_ROOT_LITERAL_RE`), so a descendant base's contiguous ``rm
        # -rf `` text matched is still denied without the token-adjacency guesswork
        # a structural flag check needs, and a WIDENED spelling base never
        # contained (``rm -fr /tmp/x``) stays allowed (Security Scope ruling).
        has_rec = has_force = has_npr = False
        end_of_options = False
        depth = 0
        operands: list[str] = []
        # Base ``main`` ran its whole-line literal ``rm -rf /.*`` / ``rm -rf ~.*``
        # against the QUOTE-NORMALIZED token re-join too (``__init__`` Pass 2),
        # so ``rm -rf "/etc"`` / ``rm  -rf /etc`` / ``rm "-rf" /etc`` were denied
        # on base. The raw-source frame-text pin only reproduces the UNQUOTED view
        # and misses them (Opus security-class). Reproduce the normalized match
        # STRUCTURALLY, but ONLY on the raw (``strip_quotes``) view: there a real
        # ``rm`` command's ``$HOME`` operand stays the literal ``$home`` spelling
        # (NOT the expanded ``/home/<user>`` the decoded frame carries), so base's
        # ``/``/``~`` contiguity still excludes ``$HOME``-descendants, and a quoted
        # ``rm`` MENTION inside a data word (``echo 'rm -rf /'``) is a single
        # ``echo`` operand — never reaching this per-``rm`` loop — so it stays
        # exonerated. base's literal was a SINGLE contiguous ``-rf`` token
        # IMMEDIATELY followed by a literal ``/``/``~`` operand; a widened spelling
        # (``-fr``, split ``-r -f``, long options), a ``--`` between, or a
        # ``$HOME``-prefixed operand all break that and stay allowed.
        base_rf_token = False
        base_rf_descendant = False
        for arg in tokens[i + 1 :]:
            operand, quote_peeled = (
                _rm_strip_surrounding_quotes_reporting(arg) if strip_quotes else (arg, False)
            )
            # Quoting a flag does NOT stop GNU ``rm`` option parsing — bash strips
            # the quotes during word expansion, so ``rm '-rf' ~`` / ``rm "-rf" ~``
            # / ``rm -r''f ~`` / ``rm \-rf ~`` all reach ``rm`` as ``-rf`` (Opus
            # security-class). Test the flag predicates against the FULLY de-quoted
            # spelling bash acts on; the raw ``arg`` keeps its quotes and would be
            # mis-read as an operand, failing open on the wipe.
            flag_tok = _rm_strip_all_quotes(arg) if strip_quotes else arg
            # A glued control operator (``/;reboot``, ``/&&id``) leaves the real
            # operand before it; classify only that head and, outside a
            # substitution, end this rm's argv at the boundary so a command glued
            # after it is not read as another operand. A separator that only
            # became bare because a surrounding quote was peeled (``';'``) was
            # QUOTED in the source — a literal filename, not a boundary — so the
            # split is suppressed for it (Opus security-class: ``rm -rf ';' /``);
            # ``_rm_operand_before_boundary`` likewise skips a backslash-ESCAPED
            # operator (``a\;b`` is one filename). The split runs ONLY on the raw
            # (``strip_quotes``) view: the decoded view's quotes/escapes are already
            # resolved by the walk, which tokenized on the REAL separators, so a
            # bare ``;`` there is a genuine terminator the walk already placed as
            # its own token — re-splitting an operand on it would truncate a
            # legitimate filename (Opus security-class).
            glued_boundary = False
            if strip_quotes and not quote_peeled and depth + _substitution_depth_delta(arg) <= 0:
                operand, glued_boundary = _rm_operand_before_boundary(operand)
            if flag_tok == "--" and not end_of_options:
                end_of_options = True
                base_rf_token = False  # ``--`` breaks base's contiguous ``rm -rf /``
            elif not end_of_options and flag_tok == "--no-preserve-root":
                has_npr = True
                base_rf_token = False
            elif not end_of_options and _rm_is_recursive_flag(flag_tok):
                has_rec = True
                if _rm_is_force_flag(flag_tok):
                    has_force = True
                    base_rf_token = flag_tok == "-rf"  # base's single ``-rf`` token
            elif not end_of_options and _rm_is_force_flag(flag_tok):
                has_force = True
                base_rf_token = False  # ``-r -f`` split is a WIDENED spelling
            elif operand:
                # The operand IMMEDIATELY after base's ``-rf`` token: base's
                # contiguous ``rm -rf /`` / ``rm -rf ~`` matched it iff it is a
                # LITERAL ``/``- or ``~``-prefixed word (NOT ``$HOME``). Only on
                # the raw view, where ``$HOME`` is unexpanded. Test the FULLY
                # de-quoted spelling too: ``rm -rf "/"etc`` keeps a leading ``"``
                # on the raw operand, yet bash runs ``rm -rf /etc`` and base's
                # re-joined ``_shell_tokens`` view (``rm -rf /etc``) DENIED it, so
                # the stripped prefix must drive the base pin (Opus security-class).
                if strip_quotes and base_rf_token:
                    stripped_op = _rm_strip_all_quotes(operand)
                    if (
                        operand.startswith("/")
                        or operand.startswith("~")
                        or stripped_op.startswith("/")
                        or stripped_op.startswith("~")
                    ):
                        base_rf_descendant = True
                base_rf_token = False
                operands.append(operand)
            depth += _substitution_depth_delta(arg)
            # A quoted-``;``/``|`` token (``';'``) or a backslash-ESCAPED operator
            # (``a\;b``) is a literal filename argument, not a command terminator —
            # ``_ends_argv`` on the RAW ``arg`` would see the bare operator and stop
            # the argv before a later ``/`` operand (Opus security-class: ``rm -rf
            # ';' /``, ``rm -rf a\;b /*``). On the raw view the terminator fires
            # only on an UNESCAPED, unpeeled operator; the decoded view (already
            # tokenized on real separators by the walk) still ends on its own bare
            # terminator token.
            if strip_quotes:
                raw_terminates = (
                    not quote_peeled and _ends_argv(arg) and _rm_unescaped_boundary(arg) is not None
                )
            else:
                raw_terminates = _ends_argv(arg)
            if depth <= 0 and (glued_boundary or raw_terminates):
                break
            depth = max(depth, 0)
        # STRUCTURAL classification denies the EXACT root/home target ITSELF — the
        # catastrophic root/home wipe in ANY flag spelling (``rm -fr /``,
        # ``rm -r -f ~``, ``rm --recursive --force $HOME``, quoted-flag ``rm '-rf'
        # /`` — all root/home ITSELF). A DESCENDANT in a WIDENED spelling (``rm -fr
        # /tmp/x``, ``rm -rf $HOME/.cache``) is NOT denied here: base ``main``'s
        # whole-line literal was the contiguous ``rm -rf /`` / ``rm -rf ~`` text,
        # which never matched those spellings, so denying their descendants newly
        # refuses legitimate scratch/cache cleanup (Security Scope ruling). base's
        # OWN ``-rf`` descendant coverage (``rm -rf /etc``, ``rm -rf "/etc"``,
        # ``rm -rf ~/.ssh``) is reproduced by ``base_rf_descendant`` above (base's
        # contiguous ``-rf`` token + a literal ``/``/``~`` operand, raw view only)
        # for a quoted/space-padded ``rm`` command, and by the frame-text pin
        # (:func:`_rm_base_literal_bare_tokens`) for a wrapper-reached unquoted
        # ``sudo rm -rf /etc``.
        root_re, home_re = _RM_ROOT_ITSELF_RE, _RM_HOME_ITSELF_RE
        # Classify each operand AND its dot-segment-normalized form, so ``/./`` /
        # ``/tmp/../`` / ``~/.`` (which the kernel resolves to root/home) are caught
        # by the exact matchers. On the RAW split (``strip_quotes``), also classify
        # the fully de-quoted spelling, so a PARTIALLY quoted ``"$HOME"/`` /
        # ``"${HOME}"/x`` keeps its ``$HOME`` anchor the matchers need (GPT
        # security-class). The de-quote is NOT applied to the decoded view, where a
        # surrounding quote the decode produced is a literal filename character
        # (``$'"/"'`` is a file named ``/``, not the root).
        candidates = list(operands) + [_rm_normalize_dot_segments(op) for op in operands]
        if strip_quotes:
            dequoted = [_rm_strip_all_quotes(op) for op in operands]
            candidates += dequoted + [_rm_normalize_dot_segments(op) for op in dequoted]
        root_target = any(root_re.fullmatch(op) for op in candidates)
        home_target = any(home_re.fullmatch(op) for op in candidates)

        # ``base_rf_descendant`` reproduces base's quote-normalized ``rm -rf /.*``
        # / ``rm -rf ~.*`` substring (``rm -rf "/etc"``, ``rm  -rf /etc``,
        # ``rm -rf "/"etc``): base's single ``-rf`` token immediately followed by a
        # LITERAL ``/``/``~`` operand, a DESCENDANT included. The prefix is read on
        # the raw AND the fully de-quoted operand, so a partially-quoted ``"/"etc``
        # (bash: ``/etc``) classifies as root (Opus security-class). Classify the
        # matching class.
        def _rf_prefixed(ch: str) -> bool:
            return any(
                op.startswith(ch) or _rm_strip_all_quotes(op).startswith(ch) for op in operands
            )

        base_rf_root = base_descendant_pin and base_rf_descendant and _rf_prefixed("/")
        base_rf_home = base_descendant_pin and base_rf_descendant and _rf_prefixed("~")
        if has_npr or (has_rec and has_force):
            if root_target or base_rf_root:
                found.add("root")
            if home_target or base_rf_home:
                found.add("home")
    return frozenset(found)
