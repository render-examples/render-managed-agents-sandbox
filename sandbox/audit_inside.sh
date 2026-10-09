#!/bin/bash
# Run inside a sandbox during a real session. Prints counts and yes/no only, never values.
echo "env_vars_named_like_keys=$(env | grep -c -E 'KEY|TOKEN|SECRET')"
echo "env_has_environment_key=$(env | grep -q '^ANTHROPIC_ENVIRONMENT_KEY=' && echo yes || echo no)"
echo "env_has_render_key=$(env | grep -q '^RENDER_API_KEY=' && echo yes || echo no)"
echo "anthropic_env_var_names=$(env | grep -o '^ANTHROPIC_[A-Z_]*' | sort | tr '\n' ' ')"
echo "secret_file_readable=$([ -r /run/claude-worker/work_secret ] && echo yes || echo no)"
hits=$(grep -rlI 'sk-ant-oat01-' / --exclude-dir=proc --exclude-dir=sys 2>/dev/null | grep -v "^$0$")
echo "files_with_environment_key_prefix=$(printf "%s" "$hits" | grep -c .)"; [ -n "$hits" ] && echo "  paths: $hits" | tr "\n" " " && echo
hits=$(grep -rlI 'sk-ant-api0' / --exclude-dir=proc --exclude-dir=sys 2>/dev/null | grep -v "^$0$")
echo "files_with_api_key_prefix=$(printf "%s" "$hits" | grep -c .)"; [ -n "$hits" ] && echo "  paths: $hits" | tr "\n" " " && echo
hits=$(grep -rlI 'rnd_[A-Za-z0-9]\{20,\}' / --exclude-dir=proc --exclude-dir=sys 2>/dev/null | grep -v "^$0$")
echo "files_with_render_key_prefix=$(printf "%s" "$hits" | grep -c .)"; [ -n "$hits" ] && echo "  paths: $hits" | tr "\n" " " && echo
pid=$(pgrep -x ant | head -1)
echo "worker_cmdline_mentions_secret_value=$(tr '\0' ' ' < /proc/$pid/cmdline | grep -q 'eyJ' && echo yes || echo no)"
echo "worker_environ_has_environment_key=$(tr '\0' '\n' < /proc/$pid/environ | grep -q '^ANTHROPIC_ENVIRONMENT_KEY=' && echo yes || echo no)"
echo "worker_memory_readable_by_root=$([ -r /proc/$pid/mem ] && echo yes || echo no)"
