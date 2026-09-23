#!/usr/bin/env bash
# Test that a user's collection grant gives them data access on a Pelican Origin,
# talking only to the Origin (no Director involved).
#
#   1. Discover the Origin's embedded issuer for the collections' namespace. Each
#      exported namespace has its own issuer at
#      <origin>/api/v1.0/issuer/ns<namespace>/, and only that issuer turns
#      collection ACLs under the namespace into storage.* scopes. Scopes it mints
#      are RELATIVE to the namespace (storage.modify:/<user>), and the Origin's
#      data plane maps them back onto /<namespace>/<user>.
#   2. Register (once) an OAuth2 client that may use the device-code grant.
#   3. Start a device-code flow. You open the printed URL, log in AS THE USER
#      UNDER TEST, and approve. The issuer turns the user's collection ACLs into
#      storage.* scopes on the minted token.
#   4. Decode the token and check the scopes cover the user's collection prefix.
#   5. PUT / GET / DELETE an object under the user's prefix on the data endpoint,
#      and confirm a PUT under another user's prefix is refused.
#
# Usage:
#   scripts/test-collection-access.sh <username> [other-username]
#
# Environment (defaults suit the local dev Origin):
#   ORIGIN_URL     web API / issuer base       default https://localhost:8444
#   DATA_URL       data-plane base             default https://localhost:8445
#   NAMESPACE      the Origin's exported namespace, which selects the issuer   default /test
#   BASE_PATH      the job's base path; collections live at BASE_PATH/<user>  default $NAMESPACE
#   ISSUER_DISCOVERY  override the discovery URL (defaults to the per-namespace issuer)
#   CURL_INSECURE  set to 1 to skip TLS verification (self-signed dev certs)
#   STATE_DIR      where the client and token are cached
#                                              default ~/.config/pelican-collection-test
#   FORCE_LOGIN    set to 1 to ignore a cached, unexpired token
set -euo pipefail

ORIGIN_URL="${ORIGIN_URL:-https://localhost:8444}"
DATA_URL="${DATA_URL:-https://localhost:8445}"
NAMESPACE="${NAMESPACE:-/test}"
BASE_PATH="${BASE_PATH:-$NAMESPACE}"
STATE_DIR="${STATE_DIR:-$HOME/.config/pelican-collection-test}"
ISSUER_DISCOVERY="${ISSUER_DISCOVERY:-$ORIGIN_URL/api/v1.0/issuer/ns${NAMESPACE%/}/.well-known/openid-configuration}"

USER_UNDER_TEST="${1:-}"
OTHER_USER="${2:-}"
if [[ -z "$USER_UNDER_TEST" ]]; then
  echo "usage: $0 <username> [other-username]" >&2
  exit 2
fi

CURL=(curl --silent --show-error --fail-with-body)
[[ "${CURL_INSECURE:-0}" == "1" ]] && CURL+=(--insecure)

for tool in curl jq; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool" >&2; exit 2; }
done

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
NS_TAG=$(tr '/' '_' <<<"${NAMESPACE%/}")
CLIENT_FILE="$STATE_DIR/client${NS_TAG}.json"
TOKEN_FILE="$STATE_DIR/token${NS_TAG}-${USER_UNDER_TEST}.jwt"
PREFIX="${BASE_PATH%/}/${USER_UNDER_TEST}"      # federation-absolute path of the user's collection
case "$PREFIX" in
  "${NAMESPACE%/}"/*) REL_PREFIX="${PREFIX#"${NAMESPACE%/}"}" ;;   # relative to the namespace issuer
  *) echo "BASE_PATH $BASE_PATH must be under NAMESPACE $NAMESPACE" >&2; exit 2 ;;
esac

pass=0; fail=0
ok()   { echo "  PASS  $*"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $*"; fail=$((fail+1)); }
step() { echo; echo "== $*"; }

b64url_decode() {
  local s="$1"; s="${s//-/+}"; s="${s//_//}"
  case $(( ${#s} % 4 )) in 2) s+="==";; 3) s+="=";; esac
  if printf '%s' "$s" | base64 --decode 2>/dev/null; then return; fi
  printf '%s' "$s" | base64 -D
}
jwt_payload() { b64url_decode "$(cut -d. -f2 <<<"$1")"; }

# ---------------------------------------------------------------- 1. discovery
step "Discovering issuer for $NAMESPACE"
echo "  discovery: $ISSUER_DISCOVERY"
DISCO=$("${CURL[@]}" "$ISSUER_DISCOVERY")
ISSUER=$(jq -r .issuer <<<"$DISCO")
REG_EP=$(jq -r .registration_endpoint <<<"$DISCO")
DEVICE_EP=$(jq -r .device_authorization_endpoint <<<"$DISCO")
TOKEN_EP=$(jq -r .token_endpoint <<<"$DISCO")
echo "  issuer:  $ISSUER"
echo "  device:  $DEVICE_EP"
echo "  token:   $TOKEN_EP"

# ---------------------------------------------------------------- 2. client
step "OAuth2 client"
if [[ -s "$CLIENT_FILE" ]]; then
  echo "  reusing $CLIENT_FILE"
else
  "${CURL[@]}" -X POST "$REG_EP" -H 'Content-Type: application/json' \
    -d '{"client_name":"collection-access-test","grant_types":["urn:ietf:params:oauth:grant-type:device_code","refresh_token"]}' \
    > "$CLIENT_FILE"
  chmod 600 "$CLIENT_FILE"
  echo "  registered new client, saved to $CLIENT_FILE"
fi
CLIENT_ID=$(jq -r .client_id "$CLIENT_FILE")
CLIENT_SECRET=$(jq -r .client_secret "$CLIENT_FILE")
echo "  client_id: $CLIENT_ID"

# ---------------------------------------------------------------- 3. token
token_is_fresh() {
  [[ -s "$TOKEN_FILE" ]] || return 1
  local exp; exp=$(jwt_payload "$(cat "$TOKEN_FILE")" | jq -r '.exp // 0')
  (( exp > $(date +%s) + 60 ))
}

step "Access token for $USER_UNDER_TEST"
if [[ "${FORCE_LOGIN:-0}" != "1" ]] && token_is_fresh; then
  echo "  reusing unexpired token in $TOKEN_FILE (FORCE_LOGIN=1 to re-login)"
else
  # Ask for the broad scopes. The issuer replaces each one with every narrower
  # scope the user actually holds, so the result shows exactly what the ACLs grant.
  DEV=$("${CURL[@]}" -X POST "$DEVICE_EP" -u "$CLIENT_ID:$CLIENT_SECRET" \
    --data-urlencode 'scope=storage.read:/ storage.modify:/ storage.create:/')
  DEVICE_CODE=$(jq -r .device_code <<<"$DEV")
  USER_CODE=$(jq -r .user_code <<<"$DEV")
  VERIFY_URL=$(jq -r '.verification_uri_complete // .verification_uri' <<<"$DEV")
  INTERVAL=$(jq -r '.interval // 5' <<<"$DEV")
  EXPIRES_IN=$(jq -r '.expires_in // 600' <<<"$DEV")

  echo
  echo "  ACTION REQUIRED"
  echo "  Open this URL in a browser and log in as: $USER_UNDER_TEST"
  echo
  echo "      $VERIFY_URL"
  echo
  echo "  user code: $USER_CODE   (expires in ${EXPIRES_IN}s)"
  echo "  Waiting for approval..."

  deadline=$(( $(date +%s) + EXPIRES_IN ))
  ACCESS_TOKEN=""
  while (( $(date +%s) < deadline )); do
    sleep "$INTERVAL"
    RESP=$(curl --silent ${CURL_INSECURE:+--insecure} -X POST "$TOKEN_EP" -u "$CLIENT_ID:$CLIENT_SECRET" \
      --data-urlencode 'grant_type=urn:ietf:params:oauth:grant-type:device_code' \
      --data-urlencode "device_code=$DEVICE_CODE")
    ERR=$(jq -r '.error // empty' <<<"$RESP")
    case "$ERR" in
      "")                    ACCESS_TOKEN=$(jq -r .access_token <<<"$RESP"); break ;;
      authorization_pending) printf '.' ;;
      slow_down)             INTERVAL=$((INTERVAL + 5)); printf 's' ;;
      *)                     echo; echo "  token endpoint error: $RESP" >&2; exit 1 ;;
    esac
  done
  echo
  [[ -n "$ACCESS_TOKEN" ]] || { echo "  timed out waiting for approval" >&2; exit 1; }
  umask 077
  printf '%s' "$ACCESS_TOKEN" > "$TOKEN_FILE"
  echo "  token saved to $TOKEN_FILE"
fi
TOKEN=$(cat "$TOKEN_FILE")

# ---------------------------------------------------------------- 4. scopes
step "Token claims"
PAYLOAD=$(jwt_payload "$TOKEN")
jq '{iss, sub, exp: (.exp | todate), scope, groups: (."wlcg.groups" // [])}' <<<"$PAYLOAD"
SCOPE=$(jq -r '.scope // ""' <<<"$PAYLOAD")

# The namespace issuer mints paths relative to the namespace; a global issuer
# would mint them federation-absolute. Accept either spelling.
for action in read modify create; do
  if grep -qE -- "(^| )storage\.$action:($REL_PREFIX|$PREFIX)( |$)" <<<"$SCOPE"; then
    ok "scope storage.$action on the user's collection"
  else
    bad "scope storage.$action:$REL_PREFIX missing"
  fi
done
REL_BASE="${BASE_PATH#"${NAMESPACE%/}"}"; REL_BASE="${REL_BASE:-/}"
if grep -qE -- "(^| )storage\.(modify|create):(/|${NAMESPACE%/}|${REL_BASE%/}|${BASE_PATH%/})( |$)" <<<"$SCOPE"; then
  bad "token has write scope on the whole base path $BASE_PATH or namespace $NAMESPACE (too broad)"
else
  ok "no write scope above the user's own prefix"
fi

# ---------------------------------------------------------------- 5. data plane
step "Data plane at $DATA_URL"
OBJ="access-test-$(date +%s).txt"
OWN="$DATA_URL$PREFIX/$OBJ"
BODY="hello from $USER_UNDER_TEST at $(date -u +%FT%TZ)"
AUTH=(-H "Authorization: Bearer $TOKEN")
http() { curl --silent --output "$2" --write-out '%{http_code}' ${CURL_INSECURE:+--insecure} "${AUTH[@]}" "${@:3}" "$1"; }

code=$(http "$OWN" /dev/null -X PUT --data-binary "$BODY")
if [[ "$code" =~ ^2 ]]; then ok "PUT $PREFIX/$OBJ -> $code"; else bad "PUT $PREFIX/$OBJ -> $code"; fi

GOT=$(mktemp); code=$(http "$OWN" "$GOT")
if [[ "$code" == 200 && "$(cat "$GOT")" == "$BODY" ]]; then ok "GET $PREFIX/$OBJ -> 200, content matches"; else bad "GET $PREFIX/$OBJ -> $code"; fi
rm -f "$GOT"

if [[ -n "$OTHER_USER" ]]; then
  OTHER="$DATA_URL${BASE_PATH%/}/$OTHER_USER/$OBJ"
  code=$(http "$OTHER" /dev/null -X PUT --data-binary "$BODY")
  if [[ "$code" == 403 || "$code" == 401 ]]; then ok "PUT to $OTHER_USER's prefix refused -> $code"; else bad "PUT to $OTHER_USER's prefix -> $code (expected 403)"; fi
fi

code=$(http "$OWN" /dev/null -X DELETE)
if [[ "$code" =~ ^2 ]]; then ok "DELETE $PREFIX/$OBJ -> $code"; else bad "DELETE $PREFIX/$OBJ -> $code"; fi

# ---------------------------------------------------------------- summary
echo
echo "== Result: $pass passed, $fail failed"
(( fail == 0 ))
