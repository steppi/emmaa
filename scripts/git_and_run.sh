#! /usr/bin/env bash
# For use running on AWS batch. User has the option
# to specify a git branch. This is to avoid having to rebuild the Emmaa docker
# every time a change is made. Instead the docker will be able to update to
# to the latest version of the specified branch. This script is extremely
# dangerous is only intended to be run in temporary environments. It will overwrite
# unstaged changes with git reset --hard

while true; do
    case "$1" in
	-b | --branch)
	    branch=$2
	    shift 2; break ;;
	-- ) shift 2; break ;;
	* ) shift; break ;;
    esac
done

echo $branch

if [ -v $branch ]; then
    # First verify branch actually exists
    echo "Attempting to change to branch $branch"
    git rev-parse --verify $branch >/dev/null 2>/dev/null
    if [["$?" != 0 ]]; then
	echo "Error: Branch $branch could not be found"
	exit 1
    fi
    # If branch is remote we will need to fetch
    remote=$(basename $branch)
    if [[ $remote != "$branch" ]]; then
	echo "Fetching from remote $remote"
	git fetch $remote
    fi
    echo "HEAD previously at " $(git rev-parse HEAD)
    git reset --hard $branch
    echo "HEAD now at " $(git rev-parse HEAD)
fi

$@
