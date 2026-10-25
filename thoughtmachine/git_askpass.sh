#!/bin/sh
# ThoughtMachine git askpass helper (W3 -- env plumbing for git auth).
#
# git runs this program when it needs a username or password and no ambient
# credential helper can answer (``credential.helper`` is cleared by
# ``thoughtmachine.git_auth_env.build_git_auth_env``).  The prompt to answer is
# passed as ``$1``.
#
# The secret is NEVER passed on a command line and never written to disk: git
# finds this script through ``GIT_ASKPASS`` and the script reads the credential
# from the environment variable named by
# ``thoughtmachine.git_auth_env.ASKPASS_SECRET_ENV``.  That single carrier is
# the one key audit records and logs have to scrub.
#
# The username answer is not a secret, so it may be overridden through
# ``TM_GIT_ASKPASS_USERNAME`` (token auth schemes generally want
# ``x-access-token`` rather than the login git would otherwise ask for); it
# defaults to ``x-access-token``.
#
# Exit status is non-zero (and nothing is printed on stdout) when the prompt is
# unrecognised or the secret is missing, so git fails closed instead of
# blocking on a terminal prompt that a host with no interactive user behind it
# can never answer.

case "${1:-}" in
    *sername*)
        printf '%s\n' "${TM_GIT_ASKPASS_USERNAME:-x-access-token}"
        exit 0
        ;;
    *assword*)
        if [ -z "${TM_GIT_ASKPASS_SECRET:-}" ]; then
            printf '%s\n' "git_askpass.sh: TM_GIT_ASKPASS_SECRET is not set" >&2
            exit 1
        fi
        printf '%s\n' "${TM_GIT_ASKPASS_SECRET}"
        exit 0
        ;;
    *)
        printf '%s\n' "git_askpass.sh: unrecognised prompt: ${1:-}" >&2
        exit 1
        ;;
esac
