# Changelog

## 0.5.1

mu 0.14: `memo` before a `{` block lambda is coloured as the modifier.

## 0.5.0

mu 0.13: `mu`, the name of the block lambda around it, is coloured as
a language variable, like `_`.

## 0.4.0

Rename (F2) for a variable, parameter, def or memo of the file. The
name is found by Python's scoping rules on the transpiled code, and each
mu occurrence is kept only when it changes that variable alone. A rename
the mu lines cannot carry, a name already used where the variable is
seen, or a line that does not transpile is refused with the reason.

## 0.3.0

mu 0.12: a sequence of dots is one operator, so the repeat `0...,` is not
coloured as a range and a pop. In `2x` the number and the name have their
own colours.

## 0.2.0

mu 0.11: a `'` directly after a name is a prime (`m'`) and is coloured
as an operator, not as the start of a string. On a bound `f`, `r` or `b`,
where the grammar sees a string, the backend colours the prime again.

## 0.1.0

First marketplace release. Highlighting, formatting, hover, completion,
go-to-definition, signature help and a debugger that steps through the
mu file.
