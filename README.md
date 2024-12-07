# GitHub Fame
Command line tool that, similar to [git-fame](https://github.com/casperdcl/git-fame), summarizes contributions to a GitHub repository based on pull requests.

It goes through pull requests from the repository and sums up changes in the PRs. You can optionally filter by account who created the PR, exclude specific PRs, and ignore certain file globs, i.e. `package-lock.json`

It assumes that only an insignificant amount of changes in a PR are not from the person who created the PR. If this doesn't hold, it doesn't produce meaningful data.

## API quota usage
To get the changes of one PR, the tool performs one API request with the GitHub API. For `N` pull requests, the tool **will issue `N` API requests. Make sure that this is not an issue for you** before using. Without an auth token, the tool will likely run into the [hourly limit of 60 requests](https://docs.github.com/en/rest/overview/resources-in-the-rest-api?apiVersion=2022-11-28#rate-limits-for-requests-from-personal-accounts).

## Auth token: Less rate limiting, access to private repos
To get more relaxed rate limiting, and thus faster execution, you can provide an auth token created via GitHub -> Settings -> Developer settings via `--token`. These are my observations:
* A classic token with the `repo/public_repo` privilege extends the rate limiting for public repositories
* A classic token with the full `repo` privilege ("Full control of private repositories") allows access to private repositories that couldn't be accessed otherwise.
  Without the correct privileges, you will get a "422 Unprocessable Entity" HTTP error for the API requests.

Keep in mind that tokens supplied via the command line will probably remain in some history file, so create a new token with just the required privileges and delete it immediately after use.

## 1000 results limit on search API
GitHub's [List Pull Request API](https://docs.github.com/en/free-pro-team@latest/rest/pulls/pulls?apiVersion=2022-11-28#list-pull-requests) doesn't support filtering by author of the pull request. If you specify an author to filter by (`--filter-author`), the tool thus uses the [Search API](https://docs.github.com/en/rest/search/search?apiVersion=2022-11-28) instead. However, this only returns the first 1000 matching elements ([example](https://api.github.com/search/issues?per_page=100&q=is:pr+repo:obsproject/obs-studio&page=11)). The tool will abort if it hits this case.

## Excluding files or pull requests
Use `--exclude-pr` to exclude pull requests by their number, e.g. pull requests that apply automatic code formatting. Use `--exclude-glob` to exclude file globs. File glob matching is currently done using [`pathlib.PurePath.match()`](https://docs.python.org/3/library/pathlib.html#pathlib.PurePath.match). This does _not_ support the `**` wildcard in CPython < 3.13, see the [PR fixing this](https://github.com/python/cpython/pull/101398) for details.
