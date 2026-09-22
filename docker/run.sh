#!/usr/bin/env bash
#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0

set -eu

usage() {
    echo "usage: $0 [--image IMAGE --ideas-dir DIR] [options] [NAME=value ...] -- command..." >&2
    exit 2
}

die() {
    echo "docker/run.sh: $*" >&2
    exit 2
}

add_env_spec() {
    local env_spec=$1
    local env_name=${env_spec%%=*}
    case "$env_spec" in
        *=*) ;;
        *) die "environment assignment must contain '=': $env_spec" ;;
    esac
    case "$env_name" in
        ""|[!A-Za-z_]*|*[!A-Za-z0-9_]*) die "invalid environment variable name: $env_name" ;;
    esac
    env_specs+=("$env_spec")
}

image=
ideas_dir=
inspect=false
tty=false
mount_specs=()
env_specs=()

while [ "$#" -gt 0 ]; do
    case "$1" in
        --image)
            [ "$#" -ge 2 ] || usage
            image=$2
            shift 2
            ;;
        --ideas-dir)
            [ "$#" -ge 2 ] || usage
            ideas_dir=$2
            shift 2
            ;;
        --workdir)
            [ "$#" -ge 2 ] || usage
            workdir=$2
            shift 2
            ;;
        --writable|--readable)
            [ "$#" -ge 2 ] || usage
            case "$1" in
                --writable) mount_kind=writable ;;
                --readable) mount_kind=readonly ;;
            esac
            mount_specs+=("$mount_kind:$2")
            shift 2
            ;;
        --env)
            [ "$#" -ge 2 ] || usage
            env_specs+=("$2")
            shift 2
            ;;
        *=*)
            add_env_spec "$1"
            shift
            ;;
        --inspect)
            inspect=true
            tty=true
            shift
            ;;
        --tty)
            tty=true
            shift
            ;;
        --)
            shift
            break
            ;;
        *)
            usage
            ;;
    esac
done

[ "$#" -gt 0 ] || usage

echo_command=true
case "$1" in
    @*)
        command_name=${1#@}
        [ -n "$command_name" ] || die "command name must not be empty"
        shift
        set -- "$command_name" "$@"
        [ "$inspect" = true ] || echo_command=false
        ;;
esac

if [ -z "$image" ]; then
    if [ -n "${workdir:-}" ]; then
        workdir=$(CDPATH= cd -- "$workdir" && pwd -P) || die "working directory does not exist: $workdir"
        cd "$workdir"
    fi
    if [ "$echo_command" = true ]; then
        echo_fd=2
        if ( : >&3 ) 2>/dev/null; then
            echo_fd=3
        fi
        if [ -t "$echo_fd" ] && [ -z "${NO_COLOR:-}" ]; then
            printf '\033[1;36m[NATIVE]\033[0m ' >&"$echo_fd"
            printf '\033[90m' >&"$echo_fd"
            printf '%q ' env "${env_specs[@]}" >&"$echo_fd"
            printf '\033[0m' >&"$echo_fd"
            printf '%q ' "$@" >&"$echo_fd"
        else
            printf '[NATIVE] ' >&"$echo_fd"
            printf '%q ' env "${env_specs[@]}" "$@" >&"$echo_fd"
        fi
        printf '\n' >&"$echo_fd"
    fi
    exec env "${env_specs[@]}" "$@"
fi

[ -n "$image" ] || usage
[ -n "$ideas_dir" ] || usage
ideas_dir=$(CDPATH= cd -- "$ideas_dir" && pwd -P) || die "IDEAS directory does not exist: $ideas_dir"
if [ -n "${workdir:-}" ]; then
    workdir=$(CDPATH= cd -- "$workdir" && pwd -P) || die "working directory does not exist: $workdir"
else
    workdir=$ideas_dir
fi

venv_dir=$ideas_dir/docker/venv
mkdir -p "$venv_dir"

resolve_mount_path() {
    local mount_path=$1
    local resolved_path
    local candidate
    case "$mount_path" in
        ""|*:* ) die "mount path must not be empty or contain ':': $mount_path" ;;
    esac
    case "$mount_path" in
        /*)
            candidate=$mount_path
            ;;
        .|..|../*|*/../*|*/..|./*|*/./*)
            die "relative mount path contains an unsafe path: $mount_path"
            ;;
        *)
            candidate=$ideas_dir/$mount_path
            ;;
    esac
    if [ -d "$candidate" ]; then
        resolved_path=$(CDPATH= cd -- "$candidate" && pwd -P) \
            || die "mount path does not exist: $mount_path"
    elif [ -f "$candidate" ]; then
        resolved_path=$(realpath -e -- "$candidate") \
            || die "mount path does not exist: $mount_path"
    else
        die "mount path does not exist: $mount_path"
    fi
    case "$mount_path" in
        /*) ;;
        *)
            case "$resolved_path" in
                "$ideas_dir"/*) ;;
                *) die "relative mount path must stay inside ideas directory: $mount_path" ;;
            esac
            ;;
    esac
    printf '%s\n' "$resolved_path"
}

resolved_mount_specs=()
for mount_spec in "${mount_specs[@]}"; do
    mount_kind=${mount_spec%%:*}
    mount_path=$(resolve_mount_path "${mount_spec#*:}")
    duplicate=false
    for resolved_mount_spec in "${resolved_mount_specs[@]}"; do
        resolved_mount_kind=${resolved_mount_spec%%:*}
        resolved_mount_path=${resolved_mount_spec#*:}
        if [ "$resolved_mount_path" = "$mount_path" ]; then
            if [ "$resolved_mount_kind" != "$mount_kind" ]; then
                die "mount directory has conflicting modes: $mount_path"
            fi
            duplicate=true
            break
        fi
    done
    if [ "$duplicate" = false ]; then
        resolved_mount_specs+=("$mount_kind:$mount_path")
    fi
done

workdir_has_mount=false
for mount_spec in "${resolved_mount_specs[@]}"; do
    if [ "${mount_spec#*:}" = "$workdir" ]; then
        workdir_has_mount=true
        break
    fi
done

run_docker() {
    local -a docker_args=(
        --rm --init
        --env no_proxy --env https_proxy --env http_proxy
        --env NO_PROXY --env HTTPS_PROXY --env HTTP_PROXY
        --env OPENROUTER_API_KEY --env OPENAI_API_KEY --env ANTHROPIC_API_KEY --env VLLM_API_KEY
        --env UV_NO_SYNC=1 --env TINI_KILL_PROCESS_GROUP=1
        --workdir "$workdir"
        --mount "type=bind,src=$ideas_dir,dst=$ideas_dir,readonly"
        --mount "type=bind,src=$venv_dir,dst=$ideas_dir/.venv"
        --mount "type=tmpfs,dst=$ideas_dir/examples"
    )
    for env_spec in "${env_specs[@]}"; do
        docker_args+=(--env "$env_spec")
    done
    if [ "$workdir" != "$ideas_dir" ] && [ "$workdir_has_mount" = false ]; then
        docker_args+=(--mount "type=tmpfs,dst=$workdir")
    fi
    for mount_spec in "${resolved_mount_specs[@]}"; do
        mount_kind=${mount_spec%%:*}
        mount_path=${mount_spec#*:}
        case "$mount_kind" in
            readonly)
                docker_args+=(--mount "type=bind,src=$mount_path,dst=$mount_path,readonly")
                ;;
            writable)
                docker_args+=(--mount "type=bind,src=$mount_path,dst=$mount_path")
                ;;
        esac
    done
    if [ "$tty" = true ]; then
        docker_args+=(-it)
    fi
    docker_run_arg_count=${#docker_args[@]}
    if [ "$inspect" = true ]; then
        docker_args+=("$image" bash -c 'bash -i; exec "$@"' -- "$@")
    else
        docker_args+=("$image" "$@")
    fi
    docker_run_arg_count=$((docker_run_arg_count + 1))
    if [ "$echo_command" = true ]; then
        echo_fd=2
        if ( : >&3 ) 2>/dev/null; then
            echo_fd=3
        fi
        if [ -t "$echo_fd" ] && [ -z "${NO_COLOR:-}" ]; then
            printf '\033[1;36m[DOCKER %s]\033[0m ' "$image" >&"$echo_fd"
        else
            printf '[DOCKER %s] ' "$image" >&"$echo_fd"
        fi
        if [ "$inspect" = true ]; then
            printf '%q ' "$@" >&"$echo_fd"
        elif [ "${VERBOSE:-0}" = 1 ]; then
            if [ -t "$echo_fd" ] && [ -z "${NO_COLOR:-}" ]; then
                printf '\033[90mdocker run ' >&"$echo_fd"
                printf '%q ' "${docker_args[@]:0:docker_run_arg_count}" >&"$echo_fd"
                printf '\033[0m' >&"$echo_fd"
                printf '%q ' "${docker_args[@]:docker_run_arg_count}" >&"$echo_fd"
            else
                printf 'docker run ' >&"$echo_fd"
                printf '%q ' "${docker_args[@]}" >&"$echo_fd"
            fi
        else
            printf '%q ' "$@" >&"$echo_fd"
        fi
        printf '\n' >&"$echo_fd"
    fi
    exec docker run "${docker_args[@]}"
}

run_docker "$@"
