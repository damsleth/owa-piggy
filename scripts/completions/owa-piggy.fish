# fish completion for owa-piggy
# Disable file completion unless a command opts back in.
complete -c owa-piggy -f
complete -c owa-piggy -n 'not __fish_seen_subcommand_from token status debug decode remaining setup reseed edge tui audiences install-owa-tools version profiles' -a token -d 'print access token (default when no command given)'
complete -c owa-piggy -n 'not __fish_seen_subcommand_from token status debug decode remaining setup reseed edge tui audiences install-owa-tools version profiles' -a status -d 'compact ISO8601 health summary (all profiles if --profile omitted)'
complete -c owa-piggy -n 'not __fish_seen_subcommand_from token status debug decode remaining setup reseed edge tui audiences install-owa-tools version profiles' -a debug -d 'dump full setup diagnostics for one profile'
complete -c owa-piggy -n 'not __fish_seen_subcommand_from token status debug decode remaining setup reseed edge tui audiences install-owa-tools version profiles' -a decode -d 'print the JWT header and payload of the current access token'
complete -c owa-piggy -n 'not __fish_seen_subcommand_from token status debug decode remaining setup reseed edge tui audiences install-owa-tools version profiles' -a remaining -d 'print minutes remaining on the current access token'
complete -c owa-piggy -n 'not __fish_seen_subcommand_from token status debug decode remaining setup reseed edge tui audiences install-owa-tools version profiles' -a setup -d 'interactive first-time setup; creates the profile if new'
complete -c owa-piggy -n 'not __fish_seen_subcommand_from token status debug decode remaining setup reseed edge tui audiences install-owa-tools version profiles' -a reseed -d 'fetch a fresh refresh token headlessly from the Edge sidecar'
complete -c owa-piggy -n 'not __fish_seen_subcommand_from token status debug decode remaining setup reseed edge tui audiences install-owa-tools version profiles' -a edge -d 'open a normal Edge window using a profile\'s sidecar session'
complete -c owa-piggy -n 'not __fish_seen_subcommand_from token status debug decode remaining setup reseed edge tui audiences install-owa-tools version profiles' -a tui -d 'interactive token-health dashboard (profiles + freshness)'
complete -c owa-piggy -n 'not __fish_seen_subcommand_from token status debug decode remaining setup reseed edge tui audiences install-owa-tools version profiles' -a audiences -d 'list all known FOCI-accessible audiences'
complete -c owa-piggy -n 'not __fish_seen_subcommand_from token status debug decode remaining setup reseed edge tui audiences install-owa-tools version profiles' -a install-owa-tools -d 'install the companion owa-tools suite via Homebrew'
complete -c owa-piggy -n 'not __fish_seen_subcommand_from token status debug decode remaining setup reseed edge tui audiences install-owa-tools version profiles' -a version -d 'print version information'
complete -c owa-piggy -n 'not __fish_seen_subcommand_from token status debug decode remaining setup reseed edge tui audiences install-owa-tools version profiles' -a profiles -d 'list / manage profiles'
complete -c owa-piggy -n '__fish_seen_subcommand_from token' -o h
complete -c owa-piggy -n '__fish_seen_subcommand_from token' -l help
complete -c owa-piggy -n '__fish_seen_subcommand_from token' -l profile
complete -c owa-piggy -n '__fish_seen_subcommand_from token' -l audience
complete -c owa-piggy -n '__fish_seen_subcommand_from token' -l scope
complete -c owa-piggy -n '__fish_seen_subcommand_from token' -l sharepoint-tenant
complete -c owa-piggy -n '__fish_seen_subcommand_from token' -l json
complete -c owa-piggy -n '__fish_seen_subcommand_from token' -l env
complete -c owa-piggy -n '__fish_seen_subcommand_from status' -o h
complete -c owa-piggy -n '__fish_seen_subcommand_from status' -l help
complete -c owa-piggy -n '__fish_seen_subcommand_from status' -l profile
complete -c owa-piggy -n '__fish_seen_subcommand_from status' -l audience
complete -c owa-piggy -n '__fish_seen_subcommand_from status' -l scope
complete -c owa-piggy -n '__fish_seen_subcommand_from status' -l sharepoint-tenant
complete -c owa-piggy -n '__fish_seen_subcommand_from status' -l json
complete -c owa-piggy -n '__fish_seen_subcommand_from status' -l verbose
complete -c owa-piggy -n '__fish_seen_subcommand_from status' -o v
complete -c owa-piggy -n '__fish_seen_subcommand_from debug' -o h
complete -c owa-piggy -n '__fish_seen_subcommand_from debug' -l help
complete -c owa-piggy -n '__fish_seen_subcommand_from debug' -l profile
complete -c owa-piggy -n '__fish_seen_subcommand_from debug' -l audience
complete -c owa-piggy -n '__fish_seen_subcommand_from debug' -l scope
complete -c owa-piggy -n '__fish_seen_subcommand_from debug' -l sharepoint-tenant
complete -c owa-piggy -n '__fish_seen_subcommand_from decode' -o h
complete -c owa-piggy -n '__fish_seen_subcommand_from decode' -l help
complete -c owa-piggy -n '__fish_seen_subcommand_from decode' -l profile
complete -c owa-piggy -n '__fish_seen_subcommand_from decode' -l audience
complete -c owa-piggy -n '__fish_seen_subcommand_from decode' -l scope
complete -c owa-piggy -n '__fish_seen_subcommand_from decode' -l sharepoint-tenant
complete -c owa-piggy -n '__fish_seen_subcommand_from remaining' -o h
complete -c owa-piggy -n '__fish_seen_subcommand_from remaining' -l help
complete -c owa-piggy -n '__fish_seen_subcommand_from remaining' -l profile
complete -c owa-piggy -n '__fish_seen_subcommand_from remaining' -l audience
complete -c owa-piggy -n '__fish_seen_subcommand_from remaining' -l scope
complete -c owa-piggy -n '__fish_seen_subcommand_from remaining' -l sharepoint-tenant
complete -c owa-piggy -n '__fish_seen_subcommand_from setup' -o h
complete -c owa-piggy -n '__fish_seen_subcommand_from setup' -l help
complete -c owa-piggy -n '__fish_seen_subcommand_from setup' -l profile
complete -c owa-piggy -n '__fish_seen_subcommand_from setup' -l email
complete -c owa-piggy -n '__fish_seen_subcommand_from setup' -l from-trough
complete -c owa-piggy -n '__fish_seen_subcommand_from setup' -l trough-tenant
complete -c owa-piggy -n '__fish_seen_subcommand_from setup' -l trough-sub
complete -c owa-piggy -n '__fish_seen_subcommand_from setup' -l user-agent
complete -c owa-piggy -n '__fish_seen_subcommand_from setup' -l sharepoint-tenant
complete -c owa-piggy -n '__fish_seen_subcommand_from setup' -l json
complete -c owa-piggy -n '__fish_seen_subcommand_from reseed' -o h
complete -c owa-piggy -n '__fish_seen_subcommand_from reseed' -l help
complete -c owa-piggy -n '__fish_seen_subcommand_from reseed' -l profile
complete -c owa-piggy -n '__fish_seen_subcommand_from reseed' -l all
complete -c owa-piggy -n '__fish_seen_subcommand_from reseed' -l scheduled
complete -c owa-piggy -n '__fish_seen_subcommand_from reseed' -l json
complete -c owa-piggy -n '__fish_seen_subcommand_from edge' -o h
complete -c owa-piggy -n '__fish_seen_subcommand_from edge' -l help
complete -c owa-piggy -n '__fish_seen_subcommand_from edge' -l profile
complete -c owa-piggy -n '__fish_seen_subcommand_from tui' -o h
complete -c owa-piggy -n '__fish_seen_subcommand_from tui' -l help
complete -c owa-piggy -n '__fish_seen_subcommand_from tui' -l profile
complete -c owa-piggy -n '__fish_seen_subcommand_from tui' -l audience
complete -c owa-piggy -n '__fish_seen_subcommand_from tui' -l scope
complete -c owa-piggy -n '__fish_seen_subcommand_from tui' -l sharepoint-tenant
complete -c owa-piggy -n '__fish_seen_subcommand_from audiences' -o h
complete -c owa-piggy -n '__fish_seen_subcommand_from audiences' -l help
complete -c owa-piggy -n '__fish_seen_subcommand_from install-owa-tools' -o h
complete -c owa-piggy -n '__fish_seen_subcommand_from install-owa-tools' -l help
complete -c owa-piggy -n '__fish_seen_subcommand_from version' -o h
complete -c owa-piggy -n '__fish_seen_subcommand_from version' -l help
complete -c owa-piggy -n '__fish_seen_subcommand_from version' -l json
complete -c owa-piggy -n '__fish_seen_subcommand_from profiles' -a list -d 'list profiles (non-interactive alias of bare `profiles`)'
complete -c owa-piggy -n '__fish_seen_subcommand_from profiles' -a set-default -d 'make <alias> the default profile'
complete -c owa-piggy -n '__fish_seen_subcommand_from profiles' -a new -d 'create a new profile (alias of `setup --profile <alias>`)'
complete -c owa-piggy -n '__fish_seen_subcommand_from profiles' -a delete -d 'remove a profile config + Edge sidecar dir'
complete -c owa-piggy -n '__fish_seen_subcommand_from profiles' -a schedule -d 'add <alias> to the shared launchd reseed schedule'
complete -c owa-piggy -n '__fish_seen_subcommand_from profiles' -a unschedule -d 'remove <alias> from the shared launchd reseed schedule'
complete -c owa-piggy -n '__fish_seen_subcommand_from profiles' -o h
complete -c owa-piggy -n '__fish_seen_subcommand_from profiles' -l help
complete -c owa-piggy -n '__fish_seen_subcommand_from profiles' -l json
