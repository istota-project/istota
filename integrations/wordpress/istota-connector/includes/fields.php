<?php
/**
 * The value model behind istota/fields-get and istota/fields-edit.
 *
 * Pure functions over plain arrays: a field definition in the shape
 * acf_get_field() returns, and a value. Nothing here calls WordPress or ACF, so
 * tests/php/fields_test.php runs it with a bare php binary. The main plugin
 * file does the WordPress half: the target, permission, acf_get_value(), ACF's
 * own validation, reference checks, update_field() and the hooks.
 *
 * The normalized form is what fields-get returns and fields-edit accepts: keys
 * are field names, every defined sub-field is present, ids are integers and a
 * value nothing is stored for is null, never false. Layout-only types (tab,
 * message, accordion) have no value and are skipped everywhere. A seamless
 * clone arrives already spliced into its parent by ACF's acf/get_fields filter,
 * so one that still reaches this module is refused rather than guessed at.
 *
 * Errors that refuse a whole edit are thrown as Istota_Fields_Error, carrying
 * the reason the skill reports (validation_error, unknown_path, mixed_fields,
 * unsupported_field) and the params naming where. Shape and row count errors
 * are collected and returned, so one refusal lists all of them.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

const ISTOTA_FIELDS_MAX_SEGMENTS = 16;
const ISTOTA_FIELDS_MAX_OPS      = 50;

// A row's identity while ops are applied, so a list keeps its counts when
// earlier ops shift its row. '#' is outside ACF's field name alphabet.
const ISTOTA_FIELDS_ROW_TAG = '#row';

const ISTOTA_FIELDS_LAYOUT_TYPES = array( 'tab', 'message', 'accordion' );

const ISTOTA_FIELDS_STRING_TYPES = array(
	'text',
	'textarea',
	'wysiwyg',
	'email',
	'url',
	'password',
	'oembed',
	'color_picker',
	'date_picker',
	'date_time_picker',
	'time_picker',
	'radio',
	'button_group',
);

class Istota_Fields_Error extends Exception {
	public $reason;
	public $params;

	public function __construct( $reason, $message, $params = array() ) {
		parent::__construct( $message );
		$this->reason = $reason;
		$this->params = $params;
	}
}

// ---------------------------------------------------------------------------
// Definitions.

function istota_fields_type( array $def ) {
	return isset( $def['type'] ) ? (string) $def['type'] : '';
}

/** Whether a field holds a value at all: named, and not a layout-only type. */
function istota_fields_has_value( array $def ) {
	return isset( $def['name'] ) && '' !== (string) $def['name']
		&& ! in_array( istota_fields_type( $def ), ISTOTA_FIELDS_LAYOUT_TYPES, true );
}

/** A definition's valued sub-fields, by name, in definition order. */
function istota_fields_sub_fields( $def ) {
	$out = array();
	if ( ! is_array( $def ) || empty( $def['sub_fields'] ) || ! is_array( $def['sub_fields'] ) ) {
		return $out;
	}
	foreach ( $def['sub_fields'] as $sub ) {
		if ( is_array( $sub ) && istota_fields_has_value( $sub ) ) {
			$out[ (string) $sub['name'] ] = $sub;
		}
	}
	return $out;
}

/** The flexible content layout named $name, or null. */
function istota_fields_layout( array $def, $name ) {
	if ( ! is_string( $name ) || empty( $def['layouts'] ) || ! is_array( $def['layouts'] ) ) {
		return null;
	}
	foreach ( $def['layouts'] as $layout ) {
		if ( is_array( $layout ) && isset( $layout['name'] ) && $layout['name'] === $name ) {
			if ( ! isset( $layout['sub_fields'] ) || ! is_array( $layout['sub_fields'] ) ) {
				$layout['sub_fields'] = array();
			}
			return $layout;
		}
	}
	return null;
}

/** Whether a relational field holds several ids rather than one. */
function istota_fields_ref_multiple( array $def ) {
	if ( 'taxonomy' === istota_fields_type( $def ) ) {
		return isset( $def['field_type'] ) && in_array( $def['field_type'], array( 'checkbox', 'multi_select' ), true );
	}
	return ! empty( $def['multiple'] );
}

/**
 * How a definition's value is shaped, one word per row of the type table.
 * `row` is the synthetic definition of one repeater or flexible content row.
 */
function istota_fields_kind( array $def ) {
	$type = istota_fields_type( $def );
	if ( in_array( $type, ISTOTA_FIELDS_STRING_TYPES, true ) ) {
		return 'string';
	}
	switch ( $type ) {
		case 'select':
			return empty( $def['multiple'] ) ? 'string' : 'strings';
		case 'checkbox':
			return 'strings';
		case 'number':
		case 'range':
			return 'number';
		case 'true_false':
			return 'bool';
		case 'image':
		case 'file':
			return 'id';
		case 'gallery':
		case 'relationship':
			return 'ids';
		case 'post_object':
		case 'page_link':
		case 'user':
		case 'taxonomy':
			return istota_fields_ref_multiple( $def ) ? 'refs' : 'ref';
		case 'link':
			return ( isset( $def['return_format'] ) && 'url' === $def['return_format'] ) ? 'link_url' : 'link';
		case 'google_map':
			return 'map';
		case 'group':
			return 'object';
		case 'clone':
			return ( isset( $def['display'] ) && 'seamless' === $def['display'] ) ? 'seamless' : 'object';
		case 'row':
			return 'row';
		case 'repeater':
		case 'flexible_content':
			return 'rows';
	}
	return 'raw';
}

/**
 * The synthetic definition of row $index of a repeater or flexible content
 * field. A flexible row's sub-fields are its layout's, read from $row.
 */
function istota_fields_row_def( array $list_def, $index, $row ) {
	$def = array(
		'type'       => 'row',
		'name'       => (string) $index,
		'label'      => '',
		'required'   => 0,
		'sub_fields' => array(),
	);
	if ( 'flexible_content' === istota_fields_type( $list_def ) ) {
		$name                 = ( is_array( $row ) && isset( $row['acf_fc_layout'] ) && is_string( $row['acf_fc_layout'] ) ) ? $row['acf_fc_layout'] : null;
		$layout               = istota_fields_layout( $list_def, $name );
		$def['flexible']      = true;
		$def['layout']        = $name;
		$def['layout_known']  = null !== $layout;
		$def['list']          = (string) $list_def['name'];
		$def['sub_fields']    = $layout ? $layout['sub_fields'] : array();
		return $def;
	}
	if ( isset( $list_def['sub_fields'] ) && is_array( $list_def['sub_fields'] ) ) {
		$def['sub_fields'] = $list_def['sub_fields'];
	}
	return $def;
}

function istota_fields_seamless_error( array $def ) {
	return new Istota_Fields_Error(
		'unsupported_field',
		sprintf( 'The field %s is a seamless clone ACF did not expand; it cannot be read or written here.', (string) $def['name'] ),
		array( 'field' => (string) $def['name'] )
	);
}

/**
 * The compact definition fields-get returns: enough to write a valid value,
 * nesting to the leaves. Layout-only fields are left out.
 */
function istota_fields_definition( array $def ) {
	$type = istota_fields_type( $def );
	$kind = istota_fields_kind( $def );
	$out  = array(
		'name'     => isset( $def['name'] ) ? (string) $def['name'] : '',
		'type'     => $type,
		'label'    => isset( $def['label'] ) ? (string) $def['label'] : '',
		'required' => ! empty( $def['required'] ),
	);
	if ( 'row' === $kind && ! empty( $def['flexible'] ) ) {
		$out['layout'] = $def['layout'];
	}
	if ( ! empty( $def['choices'] ) && is_array( $def['choices'] ) ) {
		$out['choices'] = $def['choices'];
	}
	if ( 'select' === $type ) {
		$out['multiple'] = ! empty( $def['multiple'] );
	} elseif ( 'ref' === $kind || 'refs' === $kind ) {
		$out['multiple'] = 'refs' === $kind;
	}
	foreach ( array( 'min', 'max' ) as $limit ) {
		if ( isset( $def[ $limit ] ) && '' !== $def[ $limit ] ) {
			$out[ $limit ] = is_numeric( $def[ $limit ] ) ? $def[ $limit ] + 0 : $def[ $limit ];
		}
	}
	if ( ! empty( $def['return_format'] ) ) {
		$out['return_format'] = (string) $def['return_format'];
	}
	if ( 'raw' === $kind ) {
		$out['raw'] = true;
	}
	if ( 'object' === $kind || 'row' === $kind || 'repeater' === $type ) {
		$out['sub_fields'] = array();
		foreach ( istota_fields_sub_fields( $def ) as $sub ) {
			$out['sub_fields'][] = istota_fields_definition( $sub );
		}
	}
	if ( 'flexible_content' === $type ) {
		$out['layouts'] = array();
		$layouts        = ( isset( $def['layouts'] ) && is_array( $def['layouts'] ) ) ? $def['layouts'] : array();
		foreach ( $layouts as $layout ) {
			if ( ! is_array( $layout ) || ! isset( $layout['name'] ) ) {
				continue;
			}
			$entry = array(
				'name'  => (string) $layout['name'],
				'label' => isset( $layout['label'] ) ? (string) $layout['label'] : '',
			);
			foreach ( array( 'min', 'max' ) as $limit ) {
				if ( ! empty( $layout[ $limit ] ) ) {
					$entry[ $limit ] = is_numeric( $layout[ $limit ] ) ? $layout[ $limit ] + 0 : $layout[ $limit ];
				}
			}
			$entry['sub_fields'] = array();
			foreach ( istota_fields_sub_fields( $layout ) as $sub ) {
				$entry['sub_fields'][] = istota_fields_definition( $sub );
			}
			$out['layouts'][] = $entry;
		}
	}
	return $out;
}

// ---------------------------------------------------------------------------
// Small value helpers.

function istota_fields_is_list( $value ) {
	if ( ! is_array( $value ) ) {
		return false;
	}
	$i = 0;
	foreach ( $value as $key => $unused ) {
		if ( $key !== $i ) {
			return false;
		}
		++$i;
	}
	return true;
}

/** A JSON object as decoded to an array. An empty array is either. */
function istota_fields_is_object( $value ) {
	return is_array( $value ) && ( array() === $value || ! istota_fields_is_list( $value ) );
}

function istota_fields_empty( $raw ) {
	return null === $raw || false === $raw || '' === $raw;
}

/** $raw as an integer id, or null when it is not one. 0 is returned as 0. */
function istota_fields_int( $raw ) {
	if ( is_int( $raw ) ) {
		return $raw;
	}
	if ( is_string( $raw ) && 1 === preg_match( '/^[0-9]{1,18}$/D', $raw ) ) {
		return (int) $raw;
	}
	if ( is_float( $raw ) && is_finite( $raw ) && floor( $raw ) === $raw && abs( $raw ) < 9.0E15 ) {
		return (int) $raw;
	}
	return null;
}

/** The value a sub-field of a stored group or row holds: by key, else by name. */
function istota_fields_pick( $raw, array $sub ) {
	if ( ! is_array( $raw ) ) {
		return null;
	}
	if ( isset( $sub['key'] ) && array_key_exists( $sub['key'], $raw ) ) {
		return $raw[ $sub['key'] ];
	}
	if ( array_key_exists( $sub['name'], $raw ) ) {
		return $raw[ $sub['name'] ];
	}
	return null;
}

// ---------------------------------------------------------------------------
// Normalize and denormalize.

/** One field's raw value, from acf_get_value(), in the normalized form. */
function istota_fields_normalize( array $field, $raw ) {
	switch ( istota_fields_kind( $field ) ) {
		case 'string':
			if ( is_array( $raw ) ) {
				// A single select or radio stored by an older ACF as a list.
				$first = $raw ? reset( $raw ) : null;
				if ( ! is_scalar( $first ) && null !== $first ) {
					return $raw;
				}
				$raw = $first;
			}
			if ( istota_fields_empty( $raw ) ) {
				return null;
			}
			if ( true === $raw ) {
				return '1';
			}
			return is_scalar( $raw ) ? (string) $raw : $raw;

		case 'number':
			if ( istota_fields_empty( $raw ) ) {
				return null;
			}
			if ( is_string( $raw ) && is_numeric( $raw ) ) {
				$raw = $raw + 0;
			}
			if ( is_float( $raw ) ) {
				$int = istota_fields_int( $raw );
				return null === $int ? $raw : $int;
			}
			return $raw;

		case 'bool':
			if ( null === $raw || '' === $raw ) {
				return null;
			}
			if ( is_bool( $raw ) ) {
				return $raw;
			}
			if ( is_string( $raw ) ) {
				return '0' !== $raw;
			}
			return (bool) $raw;

		case 'id':
			if ( is_array( $raw ) ) {
				$raw = isset( $raw['ID'] ) ? $raw['ID'] : ( isset( $raw['id'] ) ? $raw['id'] : $raw );
			}
			if ( istota_fields_empty( $raw ) ) {
				return null;
			}
			$id = istota_fields_int( $raw );
			if ( null === $id ) {
				// Not an id ACF would store. Kept, so a whole-field write leaves it alone.
				return $raw;
			}
			return $id > 0 ? $id : null;

		case 'ids':
		case 'refs':
			if ( istota_fields_empty( $raw ) ) {
				return array();
			}
			$out = array();
			foreach ( is_array( $raw ) ? $raw : array( $raw ) as $item ) {
				$one = istota_fields_normalize_ref( $item );
				if ( null !== $one ) {
					$out[] = $one;
				}
			}
			return $out;

		case 'ref':
			if ( is_array( $raw ) ) {
				$raw = $raw ? reset( $raw ) : null;
			}
			return istota_fields_normalize_ref( $raw );

		case 'strings':
			if ( istota_fields_empty( $raw ) ) {
				return array();
			}
			$out = array();
			foreach ( is_array( $raw ) ? $raw : array( $raw ) as $item ) {
				$out[] = is_scalar( $item ) ? (string) $item : $item;
			}
			return $out;

		case 'link':
		case 'link_url':
			if ( is_string( $raw ) && '' !== $raw ) {
				$raw = array( 'url' => $raw );
			}
			if ( ! is_array( $raw ) || empty( $raw['url'] ) ) {
				// ACF stores a link with no url as "".
				return null;
			}
			if ( 'link_url' === istota_fields_kind( $field ) ) {
				return (string) $raw['url'];
			}
			return array(
				'url'    => (string) $raw['url'],
				'title'  => isset( $raw['title'] ) ? (string) $raw['title'] : '',
				'target' => isset( $raw['target'] ) ? (string) $raw['target'] : '',
			);

		case 'map':
			return istota_fields_empty( $raw ) || array() === $raw ? null : $raw;

		case 'object':
		case 'row':
			$out = array();
			if ( ! empty( $field['flexible'] ) ) {
				$out['acf_fc_layout'] = $field['layout'];
			}
			foreach ( istota_fields_sub_fields( $field ) as $name => $sub ) {
				$out[ $name ] = istota_fields_normalize( $sub, istota_fields_pick( $raw, $sub ) );
			}
			return $out;

		case 'rows':
			$out = array();
			if ( ! is_array( $raw ) ) {
				return $out;
			}
			$flexible = 'flexible_content' === istota_fields_type( $field );
			foreach ( array_values( $raw ) as $row ) {
				if ( ! is_array( $row ) || ( $flexible && ! isset( $row['acf_fc_layout'] ) ) ) {
					continue;
				}
				$out[] = istota_fields_normalize( istota_fields_row_def( $field, count( $out ), $row ), $row );
			}
			return $out;

		case 'seamless':
			throw istota_fields_seamless_error( $field );
	}
	return $raw;
}

/** One post, user or term reference: an integer id, a URL string, or null. */
function istota_fields_normalize_ref( $raw ) {
	if ( istota_fields_empty( $raw ) ) {
		return null;
	}
	$id = istota_fields_int( $raw );
	if ( null === $id ) {
		return $raw;
	}
	return $id > 0 ? $id : null;
}

/**
 * A normalized value as update_field() takes it: names mapped back to keys.
 *
 * Every defined sub-field of a group or row is written, a null leaf as "".
 * ACF's group handler skips a sub-field whose value isset() rejects, and a
 * row shifted by an insert would otherwise keep the old row's meta at that
 * index. A boolean is written as 1 or 0, since post meta reads false back as "".
 */
function istota_fields_denormalize( array $field, $value ) {
	$kind = istota_fields_kind( $field );
	switch ( $kind ) {
		case 'object':
		case 'row':
			$out = array();
			if ( ! empty( $field['flexible'] ) ) {
				$out['acf_fc_layout'] = ( is_array( $value ) && isset( $value['acf_fc_layout'] ) ) ? $value['acf_fc_layout'] : $field['layout'];
			}
			foreach ( istota_fields_sub_fields( $field ) as $name => $sub ) {
				$key         = isset( $sub['key'] ) ? $sub['key'] : $name;
				$out[ $key ] = istota_fields_denormalize( $sub, ( is_array( $value ) && array_key_exists( $name, $value ) ) ? $value[ $name ] : null );
			}
			return $out;

		case 'rows':
			$out = array();
			if ( ! is_array( $value ) ) {
				return $out;
			}
			foreach ( array_values( $value ) as $i => $row ) {
				if ( is_array( $row ) ) {
					$out[] = istota_fields_denormalize( istota_fields_row_def( $field, $i, $row ), $row );
				}
			}
			return $out;

		case 'seamless':
			throw istota_fields_seamless_error( $field );

		case 'raw':
			return $value;

		case 'bool':
			if ( is_bool( $value ) ) {
				return $value ? 1 : 0;
			}
			break;

		case 'link_url':
			if ( is_string( $value ) && '' !== $value ) {
				return array(
					'url'    => $value,
					'title'  => '',
					'target' => '',
				);
			}
			break;
	}
	return null === $value ? '' : $value;
}

// ---------------------------------------------------------------------------
// Paths.

/**
 * A path as a list of segments: field names as strings, indices as integers,
 * and '-' (after the last row) as the last segment of an insert.
 */
function istota_fields_parse_path( $path, $allow_append = false ) {
	if ( ! is_string( $path ) || '' === $path ) {
		throw new Istota_Fields_Error( 'validation_error', 'A path is a non-empty string such as blocks/0/items/3/label.', array( 'path' => $path ) );
	}
	$parts = explode( '/', $path );
	if ( count( $parts ) > ISTOTA_FIELDS_MAX_SEGMENTS ) {
		throw new Istota_Fields_Error( 'validation_error', sprintf( 'A path has at most %d segments.', ISTOTA_FIELDS_MAX_SEGMENTS ), array( 'path' => $path ) );
	}
	$last     = count( $parts ) - 1;
	$segments = array();
	foreach ( $parts as $i => $part ) {
		if ( '-' === $part ) {
			if ( $allow_append && $i === $last && $i > 0 ) {
				$segments[] = '-';
				continue;
			}
			throw new Istota_Fields_Error( 'validation_error', '"-" is only allowed as the last segment of an insert path.', array( 'path' => $path ) );
		}
		if ( 1 === preg_match( '/^[0-9]+$/D', $part ) ) {
			if ( 0 === $i ) {
				throw new Istota_Fields_Error( 'validation_error', 'A path starts with a top-level field name.', array( 'path' => $path ) );
			}
			if ( 1 !== preg_match( '/^(0|[1-9][0-9]{0,8})$/D', $part ) ) {
				throw new Istota_Fields_Error( 'validation_error', sprintf( 'Segment %d is not an index: no leading zeros, at most nine digits.', $i + 1 ), array( 'path' => $path ) );
			}
			$segments[] = (int) $part;
			continue;
		}
		if ( 1 !== preg_match( '/^[A-Za-z0-9_-]{1,64}$/D', $part ) ) {
			throw new Istota_Fields_Error( 'validation_error', sprintf( 'Segment %d is neither a field name nor an index.', $i + 1 ), array( 'path' => $path ) );
		}
		$segments[] = $part;
	}
	return $segments;
}

function istota_fields_path_string( array $segments ) {
	return implode( '/', $segments );
}

/**
 * The child of a node at one segment, as array( definition, value ), or null
 * when the segment does not address anything there.
 */
function istota_fields_child( array $def, $value, $segment ) {
	$kind = istota_fields_kind( $def );
	if ( 'object' === $kind || 'row' === $kind ) {
		$subs = istota_fields_sub_fields( $def );
		if ( ! is_string( $segment ) || ! isset( $subs[ $segment ] ) || ! is_array( $value ) ) {
			return null;
		}
		$child = array_key_exists( $segment, $value ) ? $value[ $segment ] : null;
		return array( $subs[ $segment ], $child );
	}
	if ( 'rows' === $kind ) {
		if ( ! is_int( $segment ) || ! is_array( $value ) || $segment >= count( $value ) || ! array_key_exists( $segment, $value ) ) {
			return null;
		}
		return array( istota_fields_row_def( $def, $segment, $value[ $segment ] ), $value[ $segment ] );
	}
	return null;
}

/**
 * The node at $segments in a top-level field's value.
 *
 * array( 'found' => true, 'value' => node, 'field' => its definition ), or
 * array( 'found' => false, 'exists' => the deepest prefix that does exist ).
 */
function istota_fields_resolve( array $field, $value, array $segments ) {
	if ( ! $segments || $segments[0] !== (string) $field['name'] ) {
		return array(
			'found'  => false,
			'exists' => '',
		);
	}
	$def  = $field;
	$node = $value;
	$done = array( $segments[0] );
	$n    = count( $segments );
	for ( $i = 1; $i < $n; $i++ ) {
		$child = istota_fields_child( $def, $node, $segments[ $i ] );
		if ( null === $child ) {
			return array(
				'found'  => false,
				'exists' => istota_fields_path_string( $done ),
			);
		}
		$def    = $child[0];
		$node   = $child[1];
		$done[] = $segments[ $i ];
	}
	return array(
		'found' => true,
		'value' => $node,
		'field' => $def,
	);
}

// ---------------------------------------------------------------------------
// Shape.

/**
 * Shape errors in $value written at $path against $field's definition, as a
 * list of array( 'path' => ..., 'message' => ... ). Required is checked here:
 * a required field written as null, "" or an empty list is an error.
 */
function istota_fields_check_shape( array $field, $value, $path ) {
	$errors = array();
	istota_fields_shape_into( $field, $value, (string) $path, $errors );
	return $errors;
}

function istota_fields_shape_into( array $def, $value, $path, array &$errors ) {
	$kind = istota_fields_kind( $def );
	$name = isset( $def['name'] ) ? (string) $def['name'] : '';
	$fail = function ( $message, $at = null ) use ( &$errors, $path ) {
		$errors[] = array(
			'path'    => null === $at ? $path : $at,
			'message' => $message,
		);
	};

	if ( 'seamless' === $kind ) {
		$fail( istota_fields_seamless_error( $def )->getMessage() );
		return;
	}
	$containers = array( 'object', 'row', 'rows' );
	if ( ! empty( $def['required'] ) && ( null === $value || '' === $value || ( array() === $value && ! in_array( $kind, array( 'object', 'row', 'map', 'raw' ), true ) ) ) ) {
		$fail( sprintf( '%s is required.', $name ) );
		return;
	}
	if ( null === $value && ! in_array( $kind, array_merge( $containers, array( 'ids', 'refs', 'strings' ) ), true ) ) {
		return;
	}

	switch ( $kind ) {
		case 'string':
		case 'link_url':
			if ( ! is_string( $value ) ) {
				$fail( 'Expects a string or null.' );
			}
			return;

		case 'number':
			if ( ! is_int( $value ) && ! is_float( $value ) ) {
				$fail( 'Expects a number or null.' );
			}
			return;

		case 'bool':
			if ( ! is_bool( $value ) ) {
				$fail( 'Expects true, false or null.' );
			}
			return;

		case 'id':
			if ( ! is_int( $value ) || $value < 1 ) {
				$fail( 'Expects an attachment id (a positive integer) or null.' );
			}
			return;

		case 'ref':
			if ( ! istota_fields_ref_ok( $def, $value ) ) {
				$fail( 'Expects an id (a positive integer) or null.' );
			}
			return;

		case 'ids':
		case 'refs':
			$ok = istota_fields_is_list( $value );
			foreach ( $ok ? $value : array() as $item ) {
				$ok = $ok && ( 'ids' === $kind ? ( is_int( $item ) && $item > 0 ) : istota_fields_ref_ok( $def, $item ) );
			}
			if ( ! $ok ) {
				$fail( 'Expects a list of ids (positive integers).' );
			}
			return;

		case 'strings':
			$ok = istota_fields_is_list( $value );
			foreach ( $ok ? $value : array() as $item ) {
				$ok = $ok && is_string( $item );
			}
			if ( ! $ok ) {
				$fail( 'Expects a list of strings.' );
			}
			return;

		case 'link':
			if ( ! istota_fields_is_object( $value ) ) {
				$fail( 'Expects an object with url, title and target, or null.' );
				return;
			}
			foreach ( $value as $key => $item ) {
				if ( ! in_array( $key, array( 'url', 'title', 'target' ), true ) ) {
					$fail( sprintf( 'A link has no %s; it has url, title and target.', $key ), $path . '/' . $key );
				} elseif ( ! is_string( $item ) ) {
					$fail( 'Expects a string.', $path . '/' . $key );
				}
			}
			return;

		case 'map':
			if ( ! istota_fields_is_object( $value ) ) {
				$fail( 'Expects an object or null.' );
			}
			return;

		case 'object':
		case 'row':
			if ( ! istota_fields_is_object( $value ) ) {
				$fail( 'row' === $kind ? 'Expects a row object.' : 'Expects an object of its sub-fields.' );
				return;
			}
			$subs = istota_fields_sub_fields( $def );
			if ( ! empty( $def['flexible'] ) ) {
				if ( empty( $def['layout_known'] ) ) {
					$fail( sprintf( 'acf_fc_layout must name a layout of %s.', $def['list'] ), $path . '/acf_fc_layout' );
					return;
				}
				if ( array_key_exists( 'acf_fc_layout', $value ) && $value['acf_fc_layout'] !== $def['layout'] ) {
					$fail( 'A row\'s layout cannot be changed by set; remove the row and insert one of the new layout.', $path . '/acf_fc_layout' );
					return;
				}
			}
			foreach ( $value as $key => $item ) {
				if ( 'acf_fc_layout' === $key && ! empty( $def['flexible'] ) ) {
					continue;
				}
				if ( ! is_string( $key ) || ! isset( $subs[ $key ] ) ) {
					$fail( sprintf( '%s is not a field here.', $key ), $path . '/' . $key );
					continue;
				}
				istota_fields_shape_into( $subs[ $key ], $item, $path . '/' . $key, $errors );
			}
			return;

		case 'rows':
			if ( ! istota_fields_is_list( $value ) ) {
				$fail( 'Expects a list of rows.' );
				return;
			}
			foreach ( $value as $i => $row ) {
				istota_fields_shape_into( istota_fields_row_def( $def, $i, $row ), $row, $path . '/' . $i, $errors );
			}
			return;
	}
	// raw: a third-party type's value is the site's to judge.
}

function istota_fields_ref_ok( array $def, $value ) {
	if ( is_int( $value ) ) {
		return $value > 0;
	}
	return 'page_link' === istota_fields_type( $def ) && is_string( $value ) && '' !== $value;
}

// ---------------------------------------------------------------------------
// Applying operations.

/**
 * $value with every sub-field its definition defines present, as null when
 * absent. Leaves are left as given; shape is checked separately.
 */
function istota_fields_fill( array $def, $value ) {
	$kind = istota_fields_kind( $def );
	if ( ( 'object' === $kind || 'row' === $kind ) && is_array( $value ) ) {
		$out = array();
		if ( ! empty( $def['flexible'] ) ) {
			$out['acf_fc_layout'] = isset( $value['acf_fc_layout'] ) ? $value['acf_fc_layout'] : $def['layout'];
		}
		foreach ( istota_fields_sub_fields( $def ) as $name => $sub ) {
			$out[ $name ] = array_key_exists( $name, $value ) ? istota_fields_fill( $sub, $value[ $name ] ) : istota_fields_normalize( $sub, null );
		}
		return $out;
	}
	if ( 'rows' === $kind && istota_fields_is_list( $value ) ) {
		foreach ( $value as $i => $row ) {
			$value[ $i ] = istota_fields_fill( istota_fields_row_def( $def, $i, $row ), $row );
		}
	}
	return $value;
}

/** Required sub-fields an inserted $value leaves out, as paths. */
function istota_fields_missing_into( array $def, $value, $path, array &$missing ) {
	$kind = istota_fields_kind( $def );
	if ( ( 'object' === $kind || 'row' === $kind ) && is_array( $value ) ) {
		foreach ( istota_fields_sub_fields( $def ) as $name => $sub ) {
			if ( ! array_key_exists( $name, $value ) ) {
				if ( ! empty( $sub['required'] ) ) {
					$missing[] = $path . '/' . $name;
				}
				continue;
			}
			istota_fields_missing_into( $sub, $value[ $name ], $path . '/' . $name, $missing );
		}
		return;
	}
	if ( 'rows' === $kind && istota_fields_is_list( $value ) ) {
		foreach ( $value as $i => $row ) {
			istota_fields_missing_into( istota_fields_row_def( $def, $i, $row ), $row, $path . '/' . $i, $missing );
		}
	}
}

/** Tag every row with an identity, or strip the tags ($counter null). */
function istota_fields_tag( array $def, $value, &$counter ) {
	$kind = istota_fields_kind( $def );
	if ( 'rows' === $kind && is_array( $value ) ) {
		foreach ( $value as $i => $row ) {
			$value[ $i ] = istota_fields_tag( istota_fields_row_def( $def, $i, $row ), $row, $counter );
		}
		return $value;
	}
	if ( ( 'object' === $kind || 'row' === $kind ) && is_array( $value ) ) {
		unset( $value[ ISTOTA_FIELDS_ROW_TAG ] );
		if ( 'row' === $kind && null !== $counter ) {
			$value[ ISTOTA_FIELDS_ROW_TAG ] = $counter++;
		}
		foreach ( istota_fields_sub_fields( $def ) as $name => $sub ) {
			if ( array_key_exists( $name, $value ) ) {
				$value[ $name ] = istota_fields_tag( $sub, $value[ $name ], $counter );
			}
		}
	}
	return $value;
}

/**
 * $new with the row identities of the $old it replaces, matched by position,
 * so a set of a row or a list compares its nested lists with their own.
 */
function istota_fields_inherit_tags( array $def, $old, $new ) {
	$kind = istota_fields_kind( $def );
	if ( 'rows' === $kind && is_array( $old ) && istota_fields_is_list( $new ) ) {
		foreach ( $new as $i => $row ) {
			if ( isset( $old[ $i ] ) ) {
				$new[ $i ] = istota_fields_inherit_tags( istota_fields_row_def( $def, $i, $row ), $old[ $i ], $row );
			}
		}
		return $new;
	}
	if ( ( 'object' === $kind || 'row' === $kind ) && is_array( $old ) && is_array( $new ) ) {
		if ( 'row' === $kind && isset( $old[ ISTOTA_FIELDS_ROW_TAG ] ) ) {
			$new[ ISTOTA_FIELDS_ROW_TAG ] = $old[ ISTOTA_FIELDS_ROW_TAG ];
		}
		foreach ( istota_fields_sub_fields( $def ) as $name => $sub ) {
			if ( array_key_exists( $name, $old ) && array_key_exists( $name, $new ) ) {
				$new[ $name ] = istota_fields_inherit_tags( $sub, $old[ $name ], $new[ $name ] );
			}
		}
	}
	return $new;
}

function istota_fields_untag( array $def, $value ) {
	$none = null;
	return istota_fields_tag( $def, $value, $none );
}

/** Set the node at $segments (past the field name) to $node. */
function istota_fields_put( &$value, array $segments, $node ) {
	$ref = &$value;
	$n   = count( $segments );
	for ( $i = 1; $i < $n; $i++ ) {
		$ref = &$ref[ $segments[ $i ] ];
	}
	$ref = $node;
}

function istota_fields_op_error( $reason, $message, $label, $params = array() ) {
	return new Istota_Fields_Error( $reason, $message, array_merge( array( 'op' => $label ), $params ) );
}

/**
 * Apply $ops, in order, to $value, the normalized value of top-level $field.
 *
 * A malformed op or a path that does not resolve throws, refusing the whole
 * edit: validation_error, unknown_path (with params.exists) or mixed_fields,
 * each with params.op naming the op as ops[i]. Otherwise returns:
 *
 * - value: the new normalized value.
 * - errors: every shape and row count error, each array( op, path, message );
 *   op is null for a count error. Non-empty means the edit is refused.
 * - previous: per op, the node a set replaced or a remove removed, else null.
 * - missing_required: required sub-fields inserted rows left out, as paths.
 * - written: per set and insert, array( op, path, field, value ), for the
 *   WordPress-side checks (ACF's own validation, references) to walk.
 */
function istota_fields_apply( array $field, $value, array $ops ) {
	$name = (string) $field['name'];
	$ops  = array_values( $ops );
	if ( ! $ops || count( $ops ) > ISTOTA_FIELDS_MAX_OPS ) {
		throw new Istota_Fields_Error( 'validation_error', sprintf( 'An edit has 1 to %d operations.', ISTOTA_FIELDS_MAX_OPS ) );
	}

	// Every op is parsed, and every path checked to name this field, before any applies.
	$parsed = array();
	foreach ( $ops as $i => $op ) {
		$label = 'ops[' . $i . ']';
		if ( ! is_array( $op ) || ! isset( $op['op'] ) || ! in_array( $op['op'], array( 'set', 'insert', 'remove', 'move' ), true ) ) {
			throw istota_fields_op_error( 'validation_error', 'Each operation has an op: set, insert, remove or move.', $label );
		}
		$verb = $op['op'];
		if ( ( 'set' === $verb || 'insert' === $verb ) && ! array_key_exists( 'value', $op ) ) {
			throw istota_fields_op_error( 'validation_error', sprintf( '%s needs a value.', $verb ), $label );
		}
		if ( 'move' === $verb && ! isset( $op['from'] ) ) {
			throw istota_fields_op_error( 'validation_error', 'move needs a from path.', $label );
		}
		try {
			$path = istota_fields_parse_path( isset( $op['path'] ) ? $op['path'] : null, 'insert' === $verb );
			$from = 'move' === $verb ? istota_fields_parse_path( $op['from'] ) : null;
		} catch ( Istota_Fields_Error $e ) {
			throw istota_fields_op_error( $e->reason, $e->getMessage(), $label, $e->params );
		}
		foreach ( array( $path, $from ) as $segments ) {
			if ( null !== $segments && $segments[0] !== $name ) {
				throw istota_fields_op_error(
					'mixed_fields',
					sprintf( 'Every operation of one edit addresses the same top-level field, %s; this one addresses %s. Run one edit per field.', $name, $segments[0] ),
					$label,
					array( 'field' => $segments[0] )
				);
			}
		}
		$parsed[] = array( $verb, $path, $from, array_key_exists( 'value', $op ) ? $op['value'] : null );
	}

	$counter = 0;
	$before  = istota_fields_tag( $field, $value, $counter );
	$work    = $before;
	$result  = array(
		'value'            => null,
		'errors'           => array(),
		'previous'         => array(),
		'missing_required' => array(),
		'written'          => array(),
	);

	foreach ( $parsed as $i => $entry ) {
		list( $verb, $path, $from, $new ) = $entry;
		$label                            = 'ops[' . $i . ']';
		$where                            = istota_fields_path_string( $path );
		$previous                         = null;

		if ( 'set' === $verb ) {
			$target = istota_fields_resolve( $field, $work, $path );
			if ( ! $target['found'] ) {
				throw istota_fields_op_error( 'unknown_path', sprintf( '%s does not exist; %s is the deepest part that does.', $where, '' === $target['exists'] ? 'none' : $target['exists'] ), $label, array( 'path' => $where, 'exists' => $target['exists'] ) );
			}
			$def      = $target['field'];
			$previous = istota_fields_untag( $def, $target['value'] );
			istota_fields_collect( $result['errors'], istota_fields_check_shape( $def, $new, $where ), $label );
			// A set clears what its value leaves out, so a required sub-field left out is
			// a required field set to null, which is refused. An insert only reports it.
			$cleared = array();
			istota_fields_missing_into( $def, $new, $where, $cleared );
			foreach ( $cleared as $at ) {
				$result['errors'][] = array(
					'op'      => $label,
					'path'    => $at,
					'message' => 'Required; a set clears what it leaves out.',
				);
			}
			istota_fields_put( $work, $path, istota_fields_inherit_tags( $def, $target['value'], istota_fields_fill( $def, $new ) ) );
			$result['written'][] = array( 'op' => $i, 'path' => $where, 'field' => $def, 'value' => $new );
		} else {
			// insert, remove and move act on a row of a repeater or flexible content list.
			$parent_path = array_slice( $path, 0, -1 );
			$index       = $path[ count( $path ) - 1 ];
			if ( ! $parent_path ) {
				throw istota_fields_op_error( 'validation_error', sprintf( '%s takes the path of a row, such as %s/0.', $verb, $where ), $label, array( 'path' => $where ) );
			}
			$parent      = istota_fields_resolve( $field, $work, $parent_path );
			if ( ! $parent['found'] ) {
				throw istota_fields_op_error( 'unknown_path', sprintf( '%s does not exist; %s is the deepest part that does.', $where, '' === $parent['exists'] ? 'none' : $parent['exists'] ), $label, array( 'path' => $where, 'exists' => $parent['exists'] ) );
			}
			$list_def = $parent['field'];
			if ( 'rows' !== istota_fields_kind( $list_def ) || ! is_int( $index ) && '-' !== $index ) {
				throw istota_fields_op_error( 'validation_error', sprintf( '%s acts only on a row of a repeater or flexible content field, addressed by index. To change any other value, set it.', $verb ), $label, array( 'path' => $where ) );
			}
			$list   = is_array( $parent['value'] ) ? array_values( $parent['value'] ) : array();
			$count  = count( $list );
			$parent_where = istota_fields_path_string( $parent_path );
			$past   = function ( $at, $limit ) use ( $label, $where, $parent_where ) {
				return istota_fields_op_error( 'unknown_path', sprintf( '%s does not exist: the list at %s has %d rows.', $at, $parent_where, $limit ), $label, array( 'path' => $where, 'exists' => $parent_where ) );
			};

			if ( 'insert' === $verb ) {
				$index = '-' === $index ? $count : $index;
				if ( $index > $count ) {
					throw $past( $where, $count );
				}
				$where   = $parent_where . '/' . $index;
				$row_def = istota_fields_row_def( $list_def, $index, $new );
				istota_fields_collect( $result['errors'], istota_fields_check_shape( $row_def, $new, $where ), $label );
				istota_fields_missing_into( $row_def, $new, $where, $result['missing_required'] );
				array_splice( $list, $index, 0, array( istota_fields_fill( $row_def, $new ) ) );
				$result['written'][] = array( 'op' => $i, 'path' => $where, 'field' => $row_def, 'value' => $new );
			} elseif ( 'remove' === $verb ) {
				if ( $index >= $count ) {
					throw $past( $where, $count );
				}
				$previous = istota_fields_untag( istota_fields_row_def( $list_def, $index, $list[ $index ] ), $list[ $index ] );
				array_splice( $list, $index, 1 );
			} else {
				$source = $from[ count( $from ) - 1 ];
				if ( array_slice( $from, 0, -1 ) !== $parent_path || ! is_int( $source ) ) {
					throw istota_fields_op_error( 'validation_error', 'move takes a row within one list: from and path have the same parent and end in indices.', $label, array( 'path' => $where ) );
				}
				if ( $source >= $count ) {
					throw $past( istota_fields_path_string( $from ), $count );
				}
				if ( $index >= $count ) {
					throw $past( $where, $count );
				}
				$row = array_splice( $list, $source, 1 );
				array_splice( $list, $index, 0, $row );
			}
			istota_fields_put( $work, $parent_path, $list );
		}
		$result['previous'][] = $previous;
	}

	istota_fields_collect( $result['errors'], istota_fields_counts( $field, $before, $work ), null );
	$result['value'] = istota_fields_untag( $field, $work );
	return $result;
}

function istota_fields_collect( array &$errors, array $found, $label ) {
	foreach ( $found as $error ) {
		$errors[] = array(
			'op'      => $label,
			'path'    => $error['path'],
			'message' => $error['message'],
		);
	}
}

// ---------------------------------------------------------------------------
// Row counts.

/**
 * Row count errors: a repeater or flexible content list, or a layout's rows
 * within flexible content, that $after holds above its max or below its min.
 *
 * A list already out of range in $before is let through unless $after makes
 * it worse. Lists are matched by row identity when the rows carry the tags
 * istota_fields_apply() puts on them, else by path. A list that is new in
 * $after (in an inserted row) and empty was left out, not emptied, and is
 * not held to its min, as a left-out required field is not.
 */
function istota_fields_counts( array $field, $before, $after ) {
	$was = array();
	$now = array();
	istota_fields_count_lists( $field, $before, (string) $field['name'], (string) $field['name'], $was );
	istota_fields_count_lists( $field, $after, (string) $field['name'], (string) $field['name'], $now );

	$errors = array();
	foreach ( $now as $id => $list ) {
		$old    = isset( $was[ $id ] ) ? $was[ $id ] : null;
		$checks = array( array( $list['def'], $list['total'], null === $old ? null : $old['total'], 'rows' ) );
		foreach ( istota_fields_layout_limits( $list['def'] ) as $layout => $limits ) {
			$checks[] = array(
				$limits,
				isset( $list['layouts'][ $layout ] ) ? $list['layouts'][ $layout ] : 0,
				null === $old ? null : ( isset( $old['layouts'][ $layout ] ) ? $old['layouts'][ $layout ] : 0 ),
				sprintf( '"%s" rows', $layout ),
			);
		}
		foreach ( $checks as $check ) {
			list( $limits, $count, $old_count, $what ) = $check;
			$max = isset( $limits['max'] ) ? (int) $limits['max'] : 0;
			$min = isset( $limits['min'] ) ? (int) $limits['min'] : 0;
			if ( $max > 0 && $count > $max && ( null === $old_count || $count > $old_count ) ) {
				$errors[] = array(
					'path'    => $list['path'],
					'message' => sprintf( '%s would hold %d %s; at most %d are allowed.', $list['path'], $count, $what, $max ),
				);
			}
			$fresh_empty = null === $old_count && 0 === $list['total'];
			if ( $min > 0 && $count < $min && ! $fresh_empty && ( null === $old_count || $count < $old_count ) ) {
				$errors[] = array(
					'path'    => $list['path'],
					'message' => sprintf( '%s would hold %d %s; at least %d are required.', $list['path'], $count, $what, $min ),
				);
			}
		}
	}
	return $errors;
}

/** Each layout's own min and max, by layout name, where either is set. */
function istota_fields_layout_limits( array $def ) {
	$out = array();
	if ( 'flexible_content' !== istota_fields_type( $def ) || empty( $def['layouts'] ) || ! is_array( $def['layouts'] ) ) {
		return $out;
	}
	foreach ( $def['layouts'] as $layout ) {
		if ( is_array( $layout ) && isset( $layout['name'] ) && ( ! empty( $layout['min'] ) || ! empty( $layout['max'] ) ) ) {
			$out[ (string) $layout['name'] ] = $layout;
		}
	}
	return $out;
}

function istota_fields_count_lists( array $def, $value, $id, $path, array &$out ) {
	$kind = istota_fields_kind( $def );
	if ( 'rows' === $kind ) {
		$rows    = istota_fields_is_list( $value ) ? $value : array();
		$layouts = array();
		foreach ( $rows as $row ) {
			if ( is_array( $row ) && isset( $row['acf_fc_layout'] ) && is_string( $row['acf_fc_layout'] ) ) {
				$layouts[ $row['acf_fc_layout'] ] = ( isset( $layouts[ $row['acf_fc_layout'] ] ) ? $layouts[ $row['acf_fc_layout'] ] : 0 ) + 1;
			}
		}
		$out[ $id ] = array(
			'def'     => $def,
			'path'    => $path,
			'total'   => count( $rows ),
			'layouts' => $layouts,
		);
		foreach ( $rows as $i => $row ) {
			$row_id = ( is_array( $row ) && isset( $row[ ISTOTA_FIELDS_ROW_TAG ] ) ) ? '#' . $row[ ISTOTA_FIELDS_ROW_TAG ] : $id . '/' . $i;
			istota_fields_count_lists( istota_fields_row_def( $def, $i, $row ), $row, $row_id, $path . '/' . $i, $out );
		}
		return;
	}
	if ( ( 'object' === $kind || 'row' === $kind ) && is_array( $value ) ) {
		foreach ( istota_fields_sub_fields( $def ) as $name => $sub ) {
			if ( array_key_exists( $name, $value ) ) {
				istota_fields_count_lists( $sub, $value[ $name ], $id . '/' . $name, $path . '/' . $name, $out );
			}
		}
	}
}

// ---------------------------------------------------------------------------
// The token.

/** Object keys sorted recursively; list order kept. */
function istota_fields_sort_keys( $value ) {
	if ( is_object( $value ) ) {
		$value = get_object_vars( $value );
		if ( ! $value ) {
			return new stdClass();
		}
	}
	if ( ! is_array( $value ) ) {
		return $value;
	}
	if ( ! istota_fields_is_list( $value ) ) {
		ksort( $value, SORT_STRING );
	}
	foreach ( $value as $key => $item ) {
		$value[ $key ] = istota_fields_sort_keys( $item );
	}
	return $value;
}

function istota_fields_canonical_json( $value ) {
	// json_encode rather than wp_json_encode, so this file needs no WordPress; only
	// PHP computes the token, and invalid UTF-8 is substituted as wp_json_encode would.
	$json = json_encode( istota_fields_sort_keys( $value ), JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_INVALID_UTF8_SUBSTITUTE );
	if ( false === $json ) {
		// INF or NAN, from a stored number too large for a float. Hashing a stand-in
		// would make the token blind to edits of this field.
		throw new Istota_Fields_Error( 'unsupported_field', 'This field holds a value that cannot be encoded as JSON: ' . json_last_error_msg() . '.' );
	}
	return $json;
}

/** The token of a top-level field's normalized value: sha256: and hex. */
function istota_fields_token( $value ) {
	return 'sha256:' . hash( 'sha256', istota_fields_canonical_json( $value ) );
}
