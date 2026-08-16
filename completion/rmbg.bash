# bash completion for rmbg
# 只补模型名（只有模型名不好记/不好敲）。其余交给默认文件名补全
# （complete -o default：函数无匹配时回退 readline 默认补全）。
_rmbg() {
  local cur prev
  _init_completion || return
  case $prev in
    -m | --model | --preload-model)
      COMPREPLY=($(compgen -W "$(rmbg list 2>/dev/null)" -- "$cur"))
      ;;
  esac
}
complete -o default -F _rmbg rmbg
