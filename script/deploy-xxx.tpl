#!/usr/bin/env bash
# author: joe.zheng
# version: 24.04.02

set -e

# @self {
# @self }
NAME="${SELF#*-}"
NAME_CTL="${NAME}ctl"

VERSION="1.12.9"      # version to deploy
CUR_VER=              # current version if any
RESET=n               # reset the deployment
UNINSTALL=n           # reset the deployment and uninstall components

REPO_URL="https://github.com/cert-manager/cert-manager"
VARIANT="$(uname -s | tr A-Z a-z)-$(uname -m | sed 's/x86_64/amd64/')"
INSTALL_DIR_BIN="/usr/local/bin"

TIMEOUT="300s"
NAMESPACE="$NAME"

# @dry-run {
# @dry-run }

function usage() {
  cat <<EOF
Usage: $SELF [-m <type>] [-v <ver>] [-R] [-U] [-n] [-h]

  Deploy $NAME in Kubernetes cluster the easy way

  -m <type>: use mirror [$MIRRORS], default: $MIRROR
  -v <ver>:  target version, default: $VERSION
  -R:        reset the deployment, default: $RESET
  -U:        reset the deployment and uninstall components, default: $UNINSTALL
  -n:        print information only, default: $DRY_RUN
  -h:        print the usage message

Cache:

  To speed up deployment, the required files will be cached at "$CACHE"

Environment variables:

  MIRROR_URL:  mirror to use, default: $MIRROR_URL
  OFFLINE:     force into offline mode [y n], default: $OFFLINE

Examples:

  1. Deploy $NAME
     $SELF

  2. Reset the deployment
     $SELF -R

  3. Deploy $NAME with specific version
     $SELF -v $VERSION

  4. Reset the deployment and uninstall
     $SELF -U

Version: $SELF_VERSION
EOF

}

function current_version() {
  local version=
  if which $NAME_CTL >/dev/null; then
    version="$($NAME_CTL version --short 2>/dev/null | sed -n 's/Server Version: v\(.*\)/\1/p')"
  fi
  echo $version
}

function main() {
  # adjust default version if already deployed
  CUR_VER="$(current_version)"
  VERSION="${CUR_VER:-$VERSION}"

  while getopts ":m:v:hnRU" opt
  do
    case $opt in
      m ) MIRROR=$OPTARG
          if echo $MIRRORS | grep -v -w $MIRROR >/dev/null 2>&1; then
            err "invalid mirror option $MIRROR"
            usage && exit 1
          fi
          ;;
      v ) VERSION=${OPTARG#v};;
      R ) RESET=y;;
      U ) RESET=y && UNINSTALL=y;;
      n ) DRY_RUN=y && RUN='echo';;
      h ) usage && exit;;
      * ) usage && echo "invalid option: -$OPTARG" && exit 1;;
    esac
  done
  shift $((OPTIND-1))

  for v in CACHE DRY_RUN MIRROR MIRROR_URL OFFLINE RESET UNINSTALL VERSION CUR_VER
  do
    eval echo "$v: \${$v}"
  done

  [[ $DRY_RUN == "y" ]] && exit

  validate_sudo
  check_prerequisites
  check_network
  check_mirror
  ensure_workdir

  local manifest_dir="$CACHE_MANIFESTS/$NAME-$VERSION"
  local manifest_list="cert-manager.yaml"
  local package_dir="$CACHE_PACKAGES/$NAME-$VERSION"
  local package_list="$NAME_CTL"

  mkdir -p $package_dir $manifest_dir

  if [[ $OFFLINE == "y" ]]; then
    msg "WARNING: offline mode, cache must be ready"
  else
    msg "download manifests"
    for n in $manifest_list; do
      local u="$REPO_URL/releases/download/v$VERSION/$n"
      download_file "$(mirror_url $u)" "$manifest_dir/$n"
    done

    msg "download packages"
    for n in $package_list; do
      local f="$n-$VARIANT.tar.gz"
      local u="$REPO_URL/releases/download/v$VERSION/$f"
      download_file "$(mirror_url $u)" "$package_dir/$f"
  done
  fi

  msg "extract packages to $CACHE_RUN"
  for n in $package_list; do
    local f="$n-$VARIANT.tar.gz"
    tar xzf "$package_dir/$f" -C $CACHE_RUN $n
  done

  if [[ $RESET == "y" ]]; then
    local kd="kubectl delete --ignore-not-found"
    if [[ -n "$CUR_VER" ]]; then
      msg "clean it up first"
      $kd -A --all issuer,clusterissuer,certificate,certificaterequest,order,challenge
    fi
    msg "delete $NAME deployment (if any)"
    for m in "$manifest_list"; do
      $kd -f "$manifest_dir/$m"
    done
  else
    msg "deploy $NAME"
    for m in "$manifest_list"; do
      kubectl apply -f "$manifest_dir/$m"
    done
  fi

  if [[ $UNINSTALL == "y" ]]; then
    msg "uninstall $package_list"
    for n in "$package_list"; do
      $SUDO rm -f $INSTALL_DIR_BIN/$n
    done
  else
    msg "install $package_list"
    for n in "$package_list"; do
      $SUDO install -t $INSTALL_DIR_BIN $CACHE_RUN/$n
    done
  fi

  if [[ $RESET != "y" ]]; then
    msg "wait until ready (timeout: $TIMEOUT)"
    $NAME_CTL check api --wait=$TIMEOUT
    cat <<EOF

# here are some tips to follow:
# check current status
kubectl get all -n $NAMESPACE

# check client and server version
$NAME_CTL version

# check more details in $REPO_URL
EOF
  fi

  msg "done"
}

function check_prerequisites() {
  msg "check prerequisites"
  if [[ -z $(which kubelet) ]]; then
    err "kubelet is not available"
    exit 1
  fi
  if [[ -z $(which curl) ]]; then
    err "curl is not available"
    exit 1
  fi
  if ! kubectl version >/dev/null 2>&1; then
    err "can't access k8s cluster"
    exit 1
  fi
}

# @base {
# @base }

# @sudo {
# @sudo }

# @network {
# @network }

# @cache {
# @cache }

main "$@"
