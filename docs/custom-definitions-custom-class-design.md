# Unified custom ECHONET Lite definitions

## Scope

`scripts/custom_definitions.yaml` can define scalar ECHONET Lite properties
that are absent from MRA, including properties for an entirely custom device
class. It also supports narrowly patching MRA definitions, such as correcting
a translated enum label.

This is a build-time feature. Runtime YAML definitions, unknown manufacturer
fallbacks, structured EDT values, coefficients, and collection bindings are
out of scope.

## YAML schema

```yaml
devices:
  # An MRA-absent class needs a neutral class name.
  0x0F01:
    name_en: "Custom object 0x0F01"
    name_ja: "カスタムオブジェクト 0x0F01"
    properties:
      - epc: 0xB0
        manufacturer_code: 0x00000A
        get: optional
        format: uint8
        name_en: "Custom value"
        name_ja: "カスタム値"

  # An MRA property can be patched without restating its other fields.
  0x0134:
    properties:
      - mode: patch
        epc: 0xE0
        enum_values:
          - key: "false"
            name_en: "Heat exchanger OFF"
```

Each `devices.<class_code>` entry must contain a non-empty `properties` list.
For an MRA-absent class, `name_en` and `name_ja` are also required. The class
name must remain manufacturer-neutral because a custom class code can be
reused by different manufacturers.

## Property modes

`mode` is optional and defaults to `define`.

### `define`

`define` creates a fully specified scalar numeric or enum property. It is
allowed only when the class/EPC is absent from MRA.

Required fields:

* `epc`
* `name_en` and `name_ja`
* `get`
* Exactly one of `format` or `enum_values`

Numeric formats are limited to `uint8`, `int8`, `uint16`, `int16`, `uint32`,
and `int32`. Enum values require `edt`, `key`, `name_en`, and `name_ja`.

When MRA later adds the same class/EPC, generation fails. The MRA update must
remove the obsolete `define` or convert the needed change to a `patch` in the
same commit.

`manufacturer_code` is allowed for `define`. A definition only creates an
entity on a node with the matching manufacturer code. Multiple manufacturer
definitions can use the same EPC, but a generic and manufacturer-specific
definition cannot share an `(epc, byte_offset)`.

### `patch`

`patch` selectively updates an existing scalar MRA property. It cannot specify
`manufacturer_code`; otherwise the generic MRA entity and vendor patch would
both be created for the same node.

Unspecified fields remain MRA values. Enum patches match existing values by
`key` and can change only `name_en` and `name_ja`.

When every enum key is included in a patch, the YAML ordering becomes the
generated enum ordering. A partial enum patch retains the MRA order.

For an MRA property that expands into multiple scalar entities, `byte_offset`
is required to select the target. The offset is a selector for `patch`; it
does not change the MRA field's offset.

## Validation rules

The generator rejects:

* Invalid class codes, EPCs, manufacturer codes, access rules, roles, formats,
  numeric ranges, and non-positive `multipleOf` values.
* Empty property lists, missing names for custom classes, and unknown fields.
* A `define` for an MRA EPC, a `patch` for a non-MRA EPC, or a `patch` with a
  manufacturer code.
* Common superclass EPC definitions and patches.
* Duplicate definitions, or generic/manufacturer-specific `define` conflicts.
* Duplicate enum EDTs or keys.
* Multiple enum `define` entries for one class/EPC/manufacturer scope.
  Home Assistant's binary sensor, switch, and select entity keys do not
  include byte offsets, so these entities cannot safely coexist.
* Writable properties with a nonzero byte offset. They would require
  read-modify-write behavior that is not implemented.

## Generated identifiers

`patch` preserves the MRA-generated identifier. A `define` uses the former
custom identifier shape without the `custom` segment:

```text
class_0f01_epc_b0_00
class_0f01_epc_b0_00000a_00
```

The identifier includes class, EPC, optional manufacturer scope, and byte
offset. This keeps translation keys stable while avoiding collisions between
manufacturer-specific definitions.
