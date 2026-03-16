#!/bin/bash

scripts_dir="$(dirname "$0")"
converter_script="$scripts_dir/gpt_svg_hp.py"

svg_dir="$1"
hp_dir="$2"

if [ -z "$svg_dir" ] || [ -z "$hp_dir" ]; then
  echo "Usage: $0 <svg_dir> <hp_dir>"
  echo "Bulk converts SVG files in <svg_dir> to HPGL format and saves them in <hp_dir>"
  exit 1
fi

if [ ! -d "$svg_dir" ]; then
  echo "Error: Directory '$svg_dir' does not exist."
  exit 1
fi

[ ! -d "$hp_dir" ] && mkdir -p "$hp_dir"

svg_dir="$(realpath "$svg_dir")"
hp_dir="$(realpath "$hp_dir")"

for s in $(ls -1 "$svg_dir/" | sort -n); do
    echo "---> $s"
    python "$converter_script" "$svg_dir/$s" "$hp_dir/${s%.svg}.hp" --quantization 0.001 --simplify 0.1
done
