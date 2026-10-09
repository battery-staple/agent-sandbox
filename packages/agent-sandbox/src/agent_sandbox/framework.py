"""Declarative CLI framework for agent sandbox with built-in tab completion."""
from __future__ import annotations

import inspect
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence


@dataclass
class Option:
    flags: list[str]
    help: str = ""
    is_flag: bool = True
    default: Any = False
    dest: str | None = None
    value: Any = True

    def __post_init__(self) -> None:
        if not self.dest:
            long_flag = max(self.flags, key=len)
            self.dest = long_flag.lstrip("-").replace("-", "_")


@dataclass
class Argument:
    name: str
    help: str = ""
    required: bool = True
    default: Any = None
    nargs: str | None = None  # None (single), "*" (zero or more), "+" (one or more), "?" (optional)
    complete: Callable[[Any], list[str]] | str | None = None


@dataclass
class Command:
    name: str
    handler: Callable[..., Any]
    help: str = ""
    aliases: list[str] = field(default_factory=list)
    options: list[Option] = field(default_factory=list)
    arguments: list[Argument] = field(default_factory=list)
    custom_usage: str | None = None

    @property
    def all_names(self) -> list[str]:
        return [self.name] + self.aliases


@dataclass
class Group:
    name: str
    help: str = ""
    aliases: list[str] = field(default_factory=list)
    commands: dict[str, Command] = field(default_factory=dict)
    default_command: str | None = None
    custom_usage: str | None = None

    @property
    def all_names(self) -> list[str]:
        return [self.name] + self.aliases

    def command(
        self,
        name: str,
        aliases: list[str] | None = None,
        help: str = "",
        custom_usage: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            options = getattr(fn, "_cli_options", [])
            arguments = getattr(fn, "_cli_arguments", [])
            cmd = Command(
                name=name,
                handler=fn,
                help=help or (fn.__doc__.strip().split("\n")[0] if fn.__doc__ else ""),
                aliases=aliases or [],
                options=list(reversed(options)),
                arguments=list(reversed(arguments)),
                custom_usage=custom_usage,
            )
            self.commands[name] = cmd
            return fn

        return decorator

    def get_command(self, name: str) -> Command | None:
        if name in self.commands:
            return self.commands[name]
        for cmd in self.commands.values():
            if name in cmd.aliases:
                return cmd
        return None


def option(
    *flags: str,
    help: str = "",
    is_flag: bool = True,
    default: Any = False,
    dest: str | None = None,
    value: Any = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to declare an option/flag on a command."""
    opt = Option(
        flags=list(flags),
        help=help,
        is_flag=is_flag,
        default=default,
        dest=dest,
        value=value,
    )

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if not hasattr(fn, "_cli_options"):
            fn._cli_options = []  # type: ignore[attr-defined]
        fn._cli_options.append(opt)  # type: ignore[attr-defined]
        return fn

    return decorator


def argument(
    name: str,
    help: str = "",
    required: bool = True,
    default: Any = None,
    nargs: str | None = None,
    complete: Callable[[Any], list[str]] | str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to declare a positional argument on a command."""
    arg = Argument(
        name=name,
        help=help,
        required=required,
        default=default,
        nargs=nargs,
        complete=complete,
    )

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if not hasattr(fn, "_cli_arguments"):
            fn._cli_arguments = []  # type: ignore[attr-defined]
        fn._cli_arguments.append(arg)  # type: ignore[attr-defined]
        return fn

    return decorator


class CLIApp:
    def __init__(self, name: str = "agent-sandbox", help: str = "") -> None:
        self.name = name
        self.help = help
        self.commands: dict[str, Command] = {}
        self.groups: dict[str, Group] = {}

    def command(
        self,
        name: str,
        aliases: list[str] | None = None,
        help: str = "",
        custom_usage: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            options = getattr(fn, "_cli_options", [])
            arguments = getattr(fn, "_cli_arguments", [])
            cmd = Command(
                name=name,
                handler=fn,
                help=help or (fn.__doc__.strip().split("\n")[0] if fn.__doc__ else ""),
                aliases=aliases or [],
                options=list(reversed(options)),
                arguments=list(reversed(arguments)),
                custom_usage=custom_usage,
            )
            self.commands[name] = cmd
            return fn

        return decorator

    def group(
        self,
        name: str,
        aliases: list[str] | None = None,
        help: str = "",
        default_command: str | None = None,
        custom_usage: str | None = None,
    ) -> Group:
        grp = Group(
            name=name,
            help=help,
            aliases=aliases or [],
            default_command=default_command,
            custom_usage=custom_usage,
        )
        self.groups[name] = grp
        return grp

    def get_target(self, name: str) -> tuple[Command | None, Group | None]:
        if name in self.commands:
            return self.commands[name], None
        for cmd in self.commands.values():
            if name in cmd.aliases:
                return cmd, None

        if name in self.groups:
            return None, self.groups[name]
        for grp in self.groups.values():
            if name in grp.aliases:
                return None, grp

        return None, None

    def format_help(self) -> str:
        lines = [
            "==========================================================",
            "  Agent Sandbox Manager CLI",
            "==========================================================",
            f"Usage: {self.name} <command> [options]",
            "",
            "Commands:",
        ]

        entries: list[tuple[str, str]] = []
        # Grouped by registration order
        for cmd in self.commands.values():
            if cmd.custom_usage:
                entries.append((cmd.custom_usage, cmd.help))
                continue
            usage_parts = [cmd.name]
            for arg in cmd.arguments:
                if arg.nargs == "*":
                    usage_parts.append(f"[{arg.name}...]" if not arg.required else f"<{arg.name}...>")
                elif arg.nargs == "+":
                    usage_parts.append(f"<{arg.name}...>")
                elif arg.nargs == "?" or not arg.required:
                    usage_parts.append(f"[{arg.name}]")
                else:
                    usage_parts.append(f"<{arg.name}>")
            for opt in cmd.options:
                if opt.flags:
                    primary_flag = next((f for f in opt.flags if f.startswith("--")), opt.flags[0])
                    if primary_flag in ("--no-host-bridge", "--force", "-y"):
                        usage_parts.append(f"[{primary_flag}]")
            entries.append((" ".join(usage_parts), cmd.help))

        for grp in self.groups.values():
            if grp.custom_usage:
                entries.append((grp.custom_usage, grp.help))
                continue
            for sub_cmd in grp.commands.values():
                if sub_cmd.custom_usage:
                    entries.append((sub_cmd.custom_usage, sub_cmd.help))
                    continue
                usage_parts = [grp.name, sub_cmd.name]
                for arg in sub_cmd.arguments:
                    if arg.nargs == "*":
                        usage_parts.append(f"[{arg.name}...]" if not arg.required else f"<{arg.name}...>")
                    elif arg.nargs == "+":
                        usage_parts.append(f"<{arg.name}...>")
                    elif arg.nargs == "?" or not arg.required:
                        usage_parts.append(f"[{arg.name}]")
                    else:
                        usage_parts.append(f"<{arg.name}>")
                for opt in sub_cmd.options:
                    if opt.flags:
                        opt_str = "|".join(opt.flags)
                        usage_parts.append(f"[{opt_str}]")
                entries.append((" ".join(usage_parts), sub_cmd.help))

        max_len = 39
        if entries:
            max_len = max(max(len(e[0]) for e in entries), 39)

        for usage_str, help_str in entries:
            padding = " " * max(2, (max_len - len(usage_str) + 2))
            lines.append(f"  {usage_str}{padding}{help_str}")

        lines.append("==========================================================")
        return "\n".join(lines)

    def dispatch(self, context: Any, argv: Sequence[str]) -> int:
        if not argv or argv[0] in ("-h", "--help", "help"):
            print(self.format_help())
            return 0

        cmd_name = argv[0]
        rest = list(argv[1:])

        cmd, group = self.get_target(cmd_name)
        if group:
            sub_name = rest[0] if rest and not rest[0].startswith("-") else group.default_command
            if not sub_name:
                sub_name = "list" if "list" in group.commands else next(iter(group.commands.keys()), None)

            if not sub_name:
                print(f"[Sandbox Error] No subcommand provided for {group.name}", file=sys.stderr)
                return 1

            sub_cmd = group.get_command(sub_name)
            if not sub_cmd:
                print(f"Unknown {group.name} command: {sub_name}", file=sys.stderr)
                return 1

            sub_rest = rest[1:] if rest and rest[0] == sub_name else rest
            return self._execute_command(context, sub_cmd, sub_rest)

        elif cmd:
            return self._execute_command(context, cmd, rest)

        else:
            print(f"Unknown command: {cmd_name}", file=sys.stderr)
            return 1

    def _execute_command(self, context: Any, cmd: Command, raw_args: list[str]) -> int:
        # Determine actual handler (allows instance mocking!)
        handler = getattr(context, cmd.handler.__name__, cmd.handler)
        kwargs: dict[str, Any] = {}

        # Set option defaults
        for opt in cmd.options:
            if opt.dest and opt.dest not in kwargs:
                kwargs[opt.dest] = opt.default

        pos_tokens: list[str] = []
        i = 0
        while i < len(raw_args):
            token = raw_args[i]
            matched_option = False
            for opt in cmd.options:
                if token in opt.flags:
                    matched_option = True
                    if opt.is_flag:
                        if opt.dest:
                            kwargs[opt.dest] = opt.value
                    else:
                        if i + 1 < len(raw_args):
                            if opt.dest:
                                kwargs[opt.dest] = raw_args[i + 1]
                            i += 1
                    break
            if not matched_option:
                pos_tokens.append(token)
            i += 1

        # Map positional tokens to declared arguments
        args_to_pass: list[Any] = []
        pos_idx = 0
        for arg in cmd.arguments:
            if arg.nargs == "*":
                args_to_pass.append(pos_tokens[pos_idx:])
                pos_idx = len(pos_tokens)
            elif arg.nargs == "+":
                if pos_idx >= len(pos_tokens):
                    print(f"[Sandbox Error] Missing required argument: <{arg.name}>", file=sys.stderr)
                    return 1
                args_to_pass.append(pos_tokens[pos_idx:])
                pos_idx = len(pos_tokens)
            elif arg.nargs == "?" or not arg.required:
                val = pos_tokens[pos_idx] if pos_idx < len(pos_tokens) else arg.default
                args_to_pass.append(val)
                pos_idx += 1
            else:
                if pos_idx >= len(pos_tokens):
                    print(f"Usage: {self.name} {cmd.name} <{arg.name}>", file=sys.stderr)
                    return 1
                args_to_pass.append(pos_tokens[pos_idx])
                pos_idx += 1

        sig = inspect.signature(handler)
        params = list(sig.parameters.values())

        accepts_context = False
        if params and params[0].name in ("self", "context", "cli") and not inspect.ismethod(handler):
            accepts_context = True

        call_args: list[Any] = [context] if accepts_context else []
        call_args.extend(args_to_pass)

        call_kwargs = {}
        for k, v in kwargs.items():
            if k in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
                call_kwargs[k] = v

        res = handler(*call_args, **call_kwargs)
        return res if isinstance(res, int) else 0

    def complete(self, context: Any, shell: str, cword: int, words: list[str]) -> list[str]:
        """Resolves tab completions for Zsh or Bash dynamically."""
        idx = cword - 1 if shell == "zsh" else cword
        cur = words[idx] if idx < len(words) else ""

        # Level 1: Top-level command or group name
        if idx <= 1:
            candidates: list[tuple[str, str]] = []
            for cmd in self.commands.values():
                candidates.append((cmd.name, cmd.help))
                for alias in cmd.aliases:
                    candidates.append((alias, f"{cmd.help} (alias)"))
            for grp in self.groups.values():
                candidates.append((grp.name, grp.help))
                for alias in grp.aliases:
                    candidates.append((alias, f"{grp.help} (alias)"))

            return self._format_candidates(shell, candidates, cur)

        # Level 2+: Inside a command or group
        first_word = words[1]
        cmd, group = self.get_target(first_word)

        if group:
            if idx == 2:
                candidates = []
                for sub_cmd in group.commands.values():
                    candidates.append((sub_cmd.name, sub_cmd.help))
                    for alias in sub_cmd.aliases:
                        candidates.append((alias, f"{sub_cmd.help} (alias)"))
                return self._format_candidates(shell, candidates, cur)

            sub_name = words[2]
            sub_cmd = group.get_command(sub_name)
            if sub_cmd:
                return self._complete_command_args(context, shell, sub_cmd, idx - 3, cur, words[3:idx])

        elif cmd:
            return self._complete_command_args(context, shell, cmd, idx - 2, cur, words[2:idx])

        return []

    def _complete_command_args(
        self,
        context: Any,
        shell: str,
        cmd: Command,
        arg_idx: int,
        cur: str,
        prior_args: list[str],
    ) -> list[str]:
        candidates: list[tuple[str, str]] = []

        if cur.startswith("-"):
            for opt in cmd.options:
                for flag in opt.flags:
                    candidates.append((flag, opt.help))
            return self._format_candidates(shell, candidates, cur)

        target_arg: Argument | None = None
        current_pos = 0
        for arg in cmd.arguments:
            if arg.nargs in ("*", "+"):
                target_arg = arg
                break
            elif current_pos == arg_idx:
                target_arg = arg
                break
            current_pos += 1

        if target_arg:
            if target_arg.complete == "directories":
                return ["__DIRS__"]
            elif target_arg.complete == "files":
                return ["__FILES__"]
            elif callable(target_arg.complete):
                try:
                    vals = target_arg.complete(context)
                    for v in vals:
                        if isinstance(v, tuple):
                            candidates.append(v)
                        else:
                            candidates.append((v, ""))
                except Exception:
                    pass

        for opt in cmd.options:
            for flag in opt.flags:
                if flag not in prior_args:
                    candidates.append((flag, opt.help))

        return self._format_candidates(shell, candidates, cur)

    def _format_candidates(self, shell: str, candidates: list[tuple[str, str]], cur: str) -> list[str]:
        res: list[str] = []
        seen = set()
        for name, desc in candidates:
            if cur and not name.startswith(cur):
                continue
            if name in seen:
                continue
            seen.add(name)

            if shell == "zsh":
                if desc:
                    clean_desc = desc.replace(":", " ").replace("[", "(").replace("]", ")")
                    res.append(f"{name}:{clean_desc}")
                else:
                    res.append(name)
            else:
                res.append(name)
        return res


def generate_completion_script(shell: str, bin_name: str = "agent-sandbox") -> str:
    """Generates the lightweight shell completion adapter script."""
    func_name = f"_{bin_name.replace('-', '_')}"
    if shell == "zsh":
        return f"""#compdef {bin_name}

{func_name}() {{
    local -a completions
    local output
    output="$({bin_name} _complete zsh "$CURRENT" "${{words[@]}}" 2>/dev/null)"
    if [[ -n "$output" ]]; then
        if [[ "$output" == "__DIRS__" ]]; then
            _files -/
        elif [[ "$output" == "__FILES__" ]]; then
            _files
        else
            completions=(${{(f)output}})
            _describe -t commands '{bin_name}' completions
        fi
    fi
}}

compdef {func_name} {bin_name}
"""
    elif shell == "bash":
        return f"""{func_name}_complete() {{
    local cur prev words cword
    if declare -F _init_completion >/dev/null 2>&1; then
        _init_completion || return
    else
        cur="${{COMP_WORDS[COMP_CWORD]}}"
    fi

    local output
    output="$({bin_name} _complete bash "$COMP_CWORD" "${{COMP_WORDS[@]}}" 2>/dev/null)"

    if [[ "$output" == "__DIRS__" ]]; then
        COMPREPLY=( $(compgen -d -- "$cur") )
    elif [[ "$output" == "__FILES__" ]]; then
        COMPREPLY=( $(compgen -f -- "$cur") )
    elif [[ -n "$output" ]]; then
        local IFS=$'\\n'
        COMPREPLY=( $(compgen -W "$output" -- "$cur") )
    fi
}}

complete -F {func_name}_complete {bin_name}
"""
    else:
        raise ValueError(f"Unsupported shell: {shell}. Only 'zsh' and 'bash' are supported.")
