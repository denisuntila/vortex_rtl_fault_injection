import sys

input_file = sys.argv[1]
output_file = sys.argv[2]

with open(input_file, "r") as f:
    lines = f.readlines()

lines.sort()

with open(output_file, "w") as f:
    f.writelines(lines)