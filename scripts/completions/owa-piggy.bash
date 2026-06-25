# bash completion for owa-piggy
_owa_piggy() {
  local cur prev cmd
  cur="${COMP_WORDS[COMP_CWORD]}"
  cmd="${COMP_WORDS[1]}"
  local commands="token status debug decode remaining setup reseed edge tui audiences install-owa-tools version profiles"
  local globals="-h --help --version -v"
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=( $(compgen -W "$commands $globals" -- "$cur") )
    return
  fi
  case "$cmd" in
    token) COMPREPLY=( $(compgen -W "-h --help --profile --audience --scope --sharepoint-tenant --json --env" -- "$cur") ) ;;
    status) COMPREPLY=( $(compgen -W "-h --help --profile --audience --scope --sharepoint-tenant --json --verbose -v" -- "$cur") ) ;;
    debug) COMPREPLY=( $(compgen -W "-h --help --profile --audience --scope --sharepoint-tenant" -- "$cur") ) ;;
    decode) COMPREPLY=( $(compgen -W "-h --help --profile --audience --scope --sharepoint-tenant" -- "$cur") ) ;;
    remaining) COMPREPLY=( $(compgen -W "-h --help --profile --audience --scope --sharepoint-tenant" -- "$cur") ) ;;
    setup) COMPREPLY=( $(compgen -W "-h --help --profile --email --from-trough --trough-tenant --trough-sub --user-agent --sharepoint-tenant --json" -- "$cur") ) ;;
    reseed) COMPREPLY=( $(compgen -W "-h --help --profile --all --scheduled --json" -- "$cur") ) ;;
    edge) COMPREPLY=( $(compgen -W "-h --help --profile" -- "$cur") ) ;;
    tui) COMPREPLY=( $(compgen -W "-h --help --profile --audience --scope --sharepoint-tenant" -- "$cur") ) ;;
    audiences) COMPREPLY=( $(compgen -W "-h --help" -- "$cur") ) ;;
    install-owa-tools) COMPREPLY=( $(compgen -W "-h --help" -- "$cur") ) ;;
    version) COMPREPLY=( $(compgen -W "-h --help --json" -- "$cur") ) ;;
    profiles) COMPREPLY=( $(compgen -W "-h --help --json list set-default new delete schedule unschedule" -- "$cur") ) ;;
  esac
}
complete -F _owa_piggy owa-piggy
