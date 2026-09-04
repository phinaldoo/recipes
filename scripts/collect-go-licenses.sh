#!/bin/sh
# Run in the resolved Go module after `go mod download all`.
set -eu

destination="${1:?usage: collect-go-licenses.sh OUTPUT_DIRECTORY}"
mkdir -p "$destination"
go list -m -f '{{if .Dir}}{{.Path}} {{.Version}} {{.Dir}}{{end}}' all > "$destination/modules.txt"
while read -r module_name module_version module_directory; do
    # The temporary main module has no version or third-party content.
    [ -n "$module_directory" ] || continue
    module_destination="$destination/$module_name@$module_version"
    mkdir -p "$module_destination"
    (
        cd "$module_directory"
        find . -type f \( -iname '*license*' -o -iname '*copying*' -o -iname '*notice*' -o -iname '*copyright*' \) |
        while IFS= read -r notice; do
            mkdir -p "$module_destination/$(dirname "$notice")"
            cp "$notice" "$module_destination/$notice"
        done
    )
done < "$destination/modules.txt"
