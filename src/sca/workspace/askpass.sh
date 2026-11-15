#!/bin/sh
case "$1" in
    *Username*|*username*) printf '%s\n' "$SCA_GIT_USERNAME" ;;
    *Password*|*password*) printf '%s\n' "$SCA_GIT_PASSWORD" ;;
    *) exit 1 ;;
esac
