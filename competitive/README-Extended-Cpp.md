# Extended C++

`main.ecpp` is the only file normally edited. Running `bo` performs:

1. combined input/output data in `copier.txt` -> `in.txt` and `expected_out.txt`
2. `preamble.ecpp` + `main.ecpp` -> `main.cpp`
3. `main.cpp` -> `a.out`
4. `a.out < in.txt` -> `out.txt`
5. `judge.py` -> the original `[AC]` / `[WA]` result UI

## Commands

- `bo`: translate, build, run, and judge
- `init`: clear contest working files while keeping templates and tooling
- `cp`: copy generated `main.cpp` to the clipboard

`bo` always removes `a.out` when it finishes, including failed builds and
runtime errors. `init` clears `copier.txt`, `in.txt`, `expected_out.txt`,
`out.txt`, `main.ecpp`, `main.cpp`, and `generator.py`.

The generated `main.cpp` starts with a commented copy of the original
`main.ecpp`. This preserves the Extended C++ source in copied or submitted
transpilation results without affecting compilation. The generated C++ and
its `#line` mappings follow that comment block.

## `additional` syntax

Extensions live in `preamble.ecpp` so contest code stays short:

```cpp
template <typename S>
S additional vector<S>.sum() const {
    return accumulate(self.begin(), self.end(), S{});
}
```

They are called like regular member functions in `main.ecpp`:

```cpp
vector<int> a{1, 2, 3};
print(a.sum());
```

Inside an extension, `self` is the receiving object. Add `const` after the
parameter list for read-only extensions. The generated `main.cpp` is ordinary
C++ and should not be edited directly.

Dispatch is type-aware. If two different types have a method with the same
name, the `additional` implementation is selected only when its receiver type
matches; otherwise the original C++ member function is called unchanged.

For `vec<S>.map(x)`, `x` is used directly when it is callable with an `S`.
If that form is not valid (for example, an overloaded function name such as
`stoi`), Extended C++ automatically applies the equivalent of `map(stf(x))`.

For a min-priority queue, both the standard spelling and the shortened
two-argument spelling below are accepted. The latter is repaired during
translation by closing the `vector` before `greater<>`:

```cpp
priority_queue<pair<ll,int>,vector<pair<ll,int>,greater<>>> q
// -> priority_queue<pair<ll,int>,vector<pair<ll,int>>,greater<>> q;
```

`priority_queue.front()` is also available as an alias of the native `top()`.

## Casts and input

The casts `int(x)`, `str(x)`, `bool(x)`, and `ld(x)` are translated to the
corresponding C++ form `(type)(x)`. `input()` reads one whitespace-delimited
value and converts it to the requested type:

```cpp
int n = int(input());
str name = str(input());
bool enabled = bool(input());
ld value = ld(input());
```

`input(count)` immediately reads and returns `count` whitespace-delimited
values as a `vec<str>`:

```cpp
values = input(3)
numbers = values.map(stf(stoi))
```

`map(f)` first tries `f(element)`. If that expression is not valid, it falls
back to `f(stoi(element))`; this lets integer-taking functions operate directly
on the strings returned by `input(count)`.

## Inferred variables

`let` is translated to `auto` only when it forms a variable declaration:

```cpp
let n = int(input());       // auto n = (int)(input());
let total = values.sum();   // auto total = ...;
for (let value : values) {} // for (auto value : values) {}
let x, y, z = int(input()), int(input()), int(input());
// auto [x, y, z] = std::tuple{...};
```

Multiple variables use a structured binding, so their inferred types may
differ from one another.

At the beginning of a line, `let` may be omitted when the assigned name has
not been declared in the current scope:

```cpp
value = int(input())             // let value = ...
value = value + 1                // assignment; value already exists
[left, right] = make_pair(1, 2)  // let [left, right] = ...
[a, b] = input(2).map(stoi)      // binds elements [0] and [1]
```

For a bracketed list, every name must be new in the current scope. Explicit
C++ declarations, `let` declarations, structured bindings, function or lambda
parameters, and loop variables are tracked as existing names. Tuple-like
values use normal C++ structured binding; indexable values such as `vec` are
unpacked by index after evaluating the right-hand side once.

Function calls such as `let(x)`, and occurrences inside comments or strings,
are not rewritten.

## Automatic semicolons

Statements in `main.ecpp` receive a semicolon at the newline, so semicolons can
normally be omitted:

```cpp
let value = int(input())
print(value)
```

End a physical line with `\\` to continue it without inserting a semicolon.
The backslash itself is removed:

```cpp
print(1, 2, \
    3)
```

Existing semicolons remain valid. Blank lines, comments, preprocessor lines,
braces, labels, and control-statement headers do not receive extra semicolons.

## Implicit `solve`

If `main.ecpp` does not define `void solve() { ... }`, the whole file is
treated as the body of `solve` automatically:

```cpp
let value = int(input())
print(value)
```

Comments and strings containing the text `void solve()` do not disable this
wrapping behavior.
