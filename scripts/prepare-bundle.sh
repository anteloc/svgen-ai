#!/bin/bash

scripts_dir="$(dirname "$0")"

root_dir="$scripts_dir/.."
root_dir="$(realpath "$root_dir")"

bundle_dir="$root_dir/bundle"
hp_dir="$bundle_dir/hp"
bundle_index="$bundle_dir/index.md"
bundle_index_tmpl="$root_dir/templates/index-tmpl.md"
bundle_instructions="$bundle_dir/instructions.md"
bundle_instructions_tmpl="$root_dir/templates/instructions-tmpl.md"
converter_script="$scripts_dir/gpt_svg_hp.py"
specs_doc="$root_dir/docs/hpgl-specs.pdf"
# bundle with timestamp
timestamp=$(date +"%Y%m%d-%H%M%S")
bundle_zip="$root_dir/dist/bundle_$timestamp.zip"

src_hp_dir="$1"
num_hp=${2:-100000} # Default to max 100000 (all of them!) if not provided

if [ -z "$src_hp_dir" ]; then
  echo "Usage: $0 <hp_dir> [<num_hp_files> (default: 100000)]"
  echo "Prepares a bundle with HPGL files from SVG drawings, an index.md and instructions.md generated from templates."
  exit 1
fi

if [ ! -d "$src_hp_dir" ]; then
  echo "Error: directory '$src_hp_dir' does not exist."
  exit 1
fi

# Ask for confirmation before proceeding
echo "Preparing a new bundle with: $num_hp HPGL files from '$src_hp_dir'."

read -p "This will *delete* the existing bundle and previous hpgl files. Do you want to continue? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
  echo "Aborting."
  exit 1
fi 

[ ! -d "$hp_dir" ] && mkdir -p "$hp_dir"

rm "$hp_dir"/*.hp

# Ask the user to describe the contents of the drawings for the bundle in a few words
echo "Please describe the contents of the drawings for this bundle in a few words"
echo "Example: drawings for Zapfino font, like e.g. single chars, words and sentences in different sizes, etc."
read -p "Description: " description

for s in $(ls -1 "$src_hp_dir/" | sort -n | head -"$num_hp"); do
    echo "---> $s"
    cp "$src_hp_dir/$s" "$hp_dir/"
done

# Render templates and copy required files

# If there is already an instructions.md in the bundle dir, ask the user for confirmation before overwriting it
if [ -f "$bundle_instructions" ]; then
    read -p "An instructions.md file already exists in the bundle directory. Do you want to overwrite it? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
      cp "$bundle_instructions_tmpl" "$bundle_instructions"
    fi
fi

echo "Edit the instructions.md if needed, then save and close the file to continue with preparing the bundle."
nano "$bundle_instructions"

cat "$bundle_index_tmpl" | sed "s/{{description}}/$description/" > "$bundle_index"
find "$hp_dir"/ -name '*.hp' | sort -n | sed "s|$hp_dir/|- |" >> "$bundle_index"

echo "Edit the index.md if needed, then save and close the file to continue with preparing the bundle."
nano "$bundle_index"

cp "$specs_doc" "$bundle_dir/"
cp "$converter_script" "$bundle_dir/"

zip -r "$bundle_zip" "$bundle_dir/"

echo "Bundle prepared and zipped as: $bundle_zip"

