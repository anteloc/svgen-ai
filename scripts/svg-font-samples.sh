#!/bin/bash

scripts_dir="$(dirname "$0")"

root_dir="$scripts_dir/.."
root_dir="$(realpath "$root_dir")"

seeds_dir="$root_dir/assets/seeds"
svg_fonts_dir="$root_dir/assets/svg/fonts"

# TODO replace this with a newly created github repo for the ttf-to-svg.ts script
ttf2svg="$root_dir/../ttf2svg/ttf2svg.mjs"
ttf2svg="$(realpath "$ttf2svg")"

sample_files="common-words.txt letters-numbers.txt sentences.txt"

font_path="$1"

if [ -z "$font_path" ]; then
  echo "Usage: $0 <path_to_ttf_font_file>"
  echo "Generates SVG samples for a given TTF font file using common words, letters, numbers, and sentences."
  echo "Current sample files under assets/seeds/ are: $sample_files"
  exit 1
fi

if [ ! -f "$font_path" ]; then
  echo "Error: File '$font_path' does not exist."
  exit 1
fi

echo "Generating SVG font samples for '$font_path'..."

basefontname="$(basename "$font_path" .ttf)"

dest_dir="$svg_fonts_dir/$basefontname"

mkdir -p "$dest_dir"

for sample in $sample_files; do
    while IFS= read -r line; do
        if [ -n "$line" ]; then
            echo "Generating SVG for: '$line'"
            node "$ttf2svg" "$font_path" "$dest_dir" "$line"
        fi
    done < "$seeds_dir/$sample"
done
