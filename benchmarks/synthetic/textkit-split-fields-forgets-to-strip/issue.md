Parsed CSV cells keep their surrounding spaces.

The importer compares cells against fixed option names, and every cell written as `a , b` fails to match because of the padding. The docstring says the cells come back stripped.
