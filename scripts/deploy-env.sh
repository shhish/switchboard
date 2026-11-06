# shellcheck shell=sh
# Shared by deployment/build scripts; never source an untrusted settings file.
DEPLOY_ENV="$HOME/.config/switchboard/deploy.env"
if [ -e "$DEPLOY_ENV" ] || [ -L "$DEPLOY_ENV" ]; then
    if [ ! -f "$DEPLOY_ENV" ] || [ -L "$DEPLOY_ENV" ] ||
        [ "$(/usr/bin/stat -f '%Lp' "$DEPLOY_ENV")" != "600" ] ||
        [ "$(/usr/bin/stat -f '%u' "$DEPLOY_ENV")" != "$(id -u)" ]; then
        echo 'Refusing deploy.env: require an owned regular non-symlink 0600 file' >&2
        exit 1
    fi
    # shellcheck source=/dev/null
    . "$DEPLOY_ENV"
fi
