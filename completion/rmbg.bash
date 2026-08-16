# bash completion for rmbg
_rmbg() {
  local cur prev words cword
  _init_completion || return
  local sub="${words[1]}"
  if [[ $cword -eq 1 ]]; then
    COMPREPLY=($(compgen -W "run serve list completion --help" -- "$cur"))
    return
  fi
  case $sub in
    run)
      case $prev in
        -m | --model)
          COMPREPLY=($(compgen -W "$(rmbg list --names 2>/dev/null)" -- "$cur"))
          ;;
        -o | --output)
          _filedir -d
          ;;
        -c | --config)
          _filedir
          ;;
        *)
          COMPREPLY=($(compgen -W "-o --output -m --model --port --process_res --sensitivity --mask_blur --mask_offset --refine -c --config --help" -- "$cur"))
          _filedir
          ;;
      esac
      ;;
    serve)
      case $prev in
        --preload-model)
          COMPREPLY=($(compgen -W "$(rmbg list --names 2>/dev/null)" -- "$cur"))
          ;;
        -c | --config)
          _filedir
          ;;
        *)
          COMPREPLY=($(compgen -W "--host --port --preload-model --no-preload --idle-kill-min --idle-unload-min -c --config --help" -- "$cur"))
          ;;
      esac
      ;;
    list)
      COMPREPLY=($(compgen -W "--names -c --config" -- "$cur"))
      ;;
    completion)
      COMPREPLY=($(compgen -W "zsh bash" -- "$cur"))
      ;;
  esac
}
complete -F _rmbg rmbg
