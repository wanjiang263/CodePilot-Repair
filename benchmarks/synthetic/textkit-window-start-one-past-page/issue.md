Paged exports skip the first row of every page after the first.

Page numbers start at 0 and a page of size `n` should begin at offset `n * page`. Today the second page begins one row late, so the row at the boundary lands on two pages and another is never shown.
