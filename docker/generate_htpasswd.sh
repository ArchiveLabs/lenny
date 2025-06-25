#!/bin/sh
# Generate an Apache htpasswd file for LCP/LSD server authentication (bcrypt)
# Or: set LCP_HTPASSWD_USER and LCP_HTPASSWD_PASS in .env and run ./generate_htpasswd.sh [<output_file>]

set -e

install_htpasswd() {
  case "$(uname)" in
    Darwin) brew install httpd ;;
    Linux)
      if [ -f "/etc/debian_version" ]; then
        sudo apt-get update && sudo apt-get install -y apache2-utils
      elif [ -f "/etc/redhat-release" ]; then
        sudo yum install -y httpd-tools
      else
        echo "Please install 'htpasswd' manually."; exit 1
      fi
      ;;
    *) echo "Please install 'htpasswd' manually."; exit 1 ;;
  esac
}

command -v htpasswd >/dev/null 2>&1 || install_htpasswd

DEFAULT_OUTPUT_FILE="./readium/config/htpasswd"

if [ -n "$1" ] && ! echo "$1" | grep -q ':'; then
  OUTPUT_FILE="$1"
  shift
else
  OUTPUT_FILE="$DEFAULT_OUTPUT_FILE"
fi

mkdir -p ./readium/config

[ -f .env ] && export $(grep -v '^#' .env | xargs)

USERS="$@"
[ -z "$USERS" ] && [ -n "$HTPASSWD_USERS" ] && USERS="$HTPASSWD_USERS"
[ -z "$USERS" ] && [ -n "$LCP_HTPASSWD_USER" ] && [ -n "$LCP_HTPASSWD_PASS" ] && USERS="$LCP_HTPASSWD_USER:$LCP_HTPASSWD_PASS"

if [ -z "$USERS" ]; then
  echo "Usage: $0 [<output_file>] <username1>:<password1> [<username2>:<password2> ...]"
  echo "   or: HTPASSWD_USERS=\"user1:pass1 user2:pass2\" $0 [<output_file>]"
  echo "   or: set LCP_HTPASSWD_USER and LCP_HTPASSWD_PASS in .env and run $0 [<output_file>]"
  exit 1
fi

FIRST=1
for up in $USERS; do
  USER="$(echo $up | cut -d: -f1)"
  PASS="$(echo $up | cut -d: -f2-)"
  if [ $FIRST -eq 1 ]; then
    htpasswd -B -b -c "$OUTPUT_FILE" "$USER" "$PASS" >/dev/null 2>&1
    FIRST=0
  else
    htpasswd -B -b "$OUTPUT_FILE" "$USER" "$PASS" >/dev/null 2>&1
  fi
done

echo "[+]LCP htpasswd generated"
