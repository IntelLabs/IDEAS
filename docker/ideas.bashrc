# Color support
alias ls="ls --color=auto"
alias grep="grep --color=auto"
alias ll="ls -alF"
alias la="ls -A"
alias l="ls -CF"

# Colored prompt
PS1='\[\033[01;32m\]\u@\h\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\$ '

# History
HISTCONTROL=ignoreboth
shopt -s histappend
HISTSIZE=1000
HISTFILESIZE=2000

# Check window size after each command
shopt -s checkwinsize

# Make autocomplete: complete with filenames
complete -f make
