<?php
/**
 * Plain-PHP tests for integrations/wordpress/istota-connector/includes/fields.php.
 *
 * No framework and no WordPress: the file under test is pure. Run as
 * `php tests/php/fields_test.php`; exits 1 on any failure. Every warning and
 * deprecation is turned into a failure, so a newer PHP's notices count.
 * Driven by tests/test_wordpress_connector_php.py.
 */

error_reporting( E_ALL );
set_error_handler(
	function ( $severity, $message, $file, $line ) {
		throw new ErrorException( $message, 0, $severity, $file, $line );
	}
);

define( 'ABSPATH', __DIR__ );
require __DIR__ . '/../../integrations/wordpress/istota-connector/includes/fields.php';

$GLOBALS['failures'] = 0;
$GLOBALS['passes']   = 0;

function ok( $condition, $what ) {
	if ( $condition ) {
		$GLOBALS['passes']++;
		return;
	}
	$GLOBALS['failures']++;
	fwrite( STDERR, "FAIL: $what\n" );
}

function same( $expected, $actual, $what ) {
	if ( $expected === $actual ) {
		$GLOBALS['passes']++;
		return;
	}
	$GLOBALS['failures']++;
	fwrite( STDERR, "FAIL: $what\n  expected: " . var_export( $expected, true ) . "\n  actual:   " . var_export( $actual, true ) . "\n" );
}

/** The Istota_Fields_Error $fn throws, or null when it returns. */
function raised( $fn ) {
	try {
		$fn();
	} catch ( Istota_Fields_Error $e ) {
		return $e;
	}
	return null;
}

function test( $name, $fn ) {
	try {
		$fn();
	} catch ( Throwable $e ) {
		$GLOBALS['failures']++;
		fwrite( STDERR, "FAIL: $name raised " . get_class( $e ) . ': ' . $e->getMessage() . ' at ' . $e->getFile() . ':' . $e->getLine() . "\n" );
	}
}

// ---------------------------------------------------------------------------
// Fixtures: definitions in the shape acf_get_field() returns, modelled on the
// `blocks` field of a real theme, where seamless clones arrive already spliced in with
// composite keys and layouts carry empty-named tab fields.

function f( $type, $name, $extra = array() ) {
	return array_merge(
		array(
			'key'      => 'field_' . $name,
			'name'     => $name,
			'label'    => ucfirst( $name ),
			'type'     => $type,
			'required' => 0,
		),
		$extra
	);
}

function items_field() {
	return f(
		'repeater',
		'items',
		array(
			'min'        => '',
			'max'        => 4,
			'sub_fields' => array(
				f( 'text', 'label', array( 'required' => 1 ) ),
				f( 'link', 'link', array( 'return_format' => 'array' ) ),
				f( 'true_false', 'done' ),
			),
		)
	);
}

function options_group() {
	// A seamless clone of a group, as SCF splices it: its own name, a composite key.
	return array(
		'key'        => 'field_64f0a1b2c3d4e_field_5f1e2d3c4b5a6',
		'name'       => 'options',
		'label'      => 'Options',
		'type'       => 'group',
		'required'   => 0,
		'sub_fields' => array(
			array( 'key' => 'field_64f0a1b2c3d4e_field_bg', 'name' => 'background', 'label' => 'Background', 'type' => 'color_picker', 'required' => 0 ),
			array( 'key' => 'field_64f0a1b2c3d4e_field_sp', 'name' => 'spacing', 'label' => 'Spacing', 'type' => 'select', 'required' => 0, 'multiple' => 0, 'choices' => array( 'sm' => 'Small', 'lg' => 'Large' ) ),
		),
	);
}

function blocks_field() {
	return f(
		'flexible_content',
		'blocks',
		array(
			'min'     => '',
			'max'     => 5,
			'layouts' => array(
				'layout_list' => array(
					'key'        => 'layout_list',
					'name'       => 'list',
					'label'      => 'List',
					'min'        => '',
					'max'        => '',
					'sub_fields' => array(
						array( 'key' => 'field_tab1', 'name' => '', 'label' => 'Content', 'type' => 'tab' ),
						f( 'text', 'title' ),
						items_field(),
						array( 'key' => 'field_msg', 'name' => 'note', 'label' => 'Note', 'type' => 'message' ),
						options_group(),
					),
				),
				'layout_text' => array(
					'key'        => 'layout_text',
					'name'       => 'text',
					'label'      => 'Text',
					'min'        => '',
					'max'        => 2,
					'sub_fields' => array(
						f( 'wysiwyg', 'body', array( 'required' => 1 ) ),
						f( 'image', 'image' ),
					),
				),
				'layout_hero' => array(
					'key'        => 'layout_hero',
					'name'       => 'hero',
					'label'      => 'Hero',
					'min'        => '',
					'max'        => '',
					'sub_fields' => array(
						f( 'image', 'image', array( 'required' => 1 ) ),
					),
				),
			),
		)
	);
}

/** One `list` row as acf_get_value() returns it: keyed by sub-field key. */
function raw_list_row( $title, $labels ) {
	$items = array();
	foreach ( $labels as $label ) {
		$items[] = array(
			'field_label' => $label,
			'field_link'  => '',
			'field_done'  => '0',
		);
	}
	return array(
		'acf_fc_layout'                          => 'list',
		'field_title'                            => $title,
		'field_items'                            => $items,
		'field_64f0a1b2c3d4e_field_5f1e2d3c4b5a6' => array(
			'field_64f0a1b2c3d4e_field_bg' => '#fff',
			'field_64f0a1b2c3d4e_field_sp' => 'sm',
		),
	);
}

function raw_blocks() {
	return array(
		raw_list_row( 'First', array( 'a', 'b', 'c' ) ),
		array(
			'acf_fc_layout' => 'text',
			'field_body'    => '<p>Hi</p>',
			'field_image'   => '',
		),
		raw_list_row( 'Second', array( 'x' ) ),
	);
}

function blocks_value() {
	return istota_fields_normalize( blocks_field(), raw_blocks() );
}

function people_field() {
	return f(
		'repeater',
		'people',
		array(
			'min'        => 1,
			'max'        => 3,
			'sub_fields' => array( f( 'text', 'name' ), f( 'number', 'age' ) ),
		)
	);
}

function people( $names ) {
	$rows = array();
	foreach ( $names as $name ) {
		$rows[] = array(
			'name' => $name,
			'age'  => null,
		);
	}
	return $rows;
}

/** The labels of the items of block $i, to read a list at a glance. */
function labels( $blocks, $i ) {
	$out = array();
	foreach ( $blocks[ $i ]['items'] as $item ) {
		$out[] = $item['label'];
	}
	return $out;
}

function titles( $blocks ) {
	$out = array();
	foreach ( $blocks as $row ) {
		$out[] = 'list' === $row['acf_fc_layout'] ? $row['title'] : $row['acf_fc_layout'];
	}
	return $out;
}

function apply_ok( $field, $value, $ops ) {
	$result = istota_fields_apply( $field, $value, $ops );
	same( array(), $result['errors'], 'apply raised no errors for ' . json_encode( $ops ) );
	return $result;
}

// ---------------------------------------------------------------------------
// A stand-in for ACF storage: what update_field() of a denormalized value
// leaves in post meta, read back with acf_get_value(). It follows the SCF
// update_value / load_value handlers this module's contract rests on: meta
// stores scalars as strings, a group skips a sub-field isset() rejects, a
// repeater writes array_key_exists sub-fields and reads back false for no
// rows, an empty flexible content field is stored as "".

function fake_meta( $value ) {
	if ( null === $value || false === $value ) {
		return '';
	}
	if ( true === $value ) {
		return '1';
	}
	if ( is_scalar( $value ) ) {
		return (string) $value;
	}
	return $value;
}

function fake_store( array $field, $value ) {
	switch ( $field['type'] ) {
		case 'group':
		case 'clone':
			$out = array();
			foreach ( istota_fields_sub_fields( $field ) as $sub ) {
				$out[ $sub['key'] ] = ( is_array( $value ) && isset( $value[ $sub['key'] ] ) ) ? fake_store( $sub, $value[ $sub['key'] ] ) : null;
			}
			return $out;
		case 'repeater':
			if ( ! is_array( $value ) || ! $value ) {
				return false;
			}
			$rows = array();
			foreach ( $value as $row ) {
				$stored = array();
				foreach ( istota_fields_sub_fields( $field ) as $sub ) {
					$stored[ $sub['key'] ] = array_key_exists( $sub['key'], $row ) ? fake_store( $sub, $row[ $sub['key'] ] ) : null;
				}
				$rows[] = $stored;
			}
			return $rows;
		case 'flexible_content':
			if ( ! is_array( $value ) || ! $value ) {
				return '';
			}
			$rows = array();
			foreach ( $value as $row ) {
				$layout = istota_fields_layout( $field, $row['acf_fc_layout'] );
				$stored = array( 'acf_fc_layout' => $row['acf_fc_layout'] );
				foreach ( istota_fields_sub_fields( $layout ) as $sub ) {
					$stored[ $sub['key'] ] = array_key_exists( $sub['key'], $row ) ? fake_store( $sub, $row[ $sub['key'] ] ) : null;
				}
				$rows[] = $stored;
			}
			return $rows;
		case 'link':
			return ( ! is_array( $value ) || empty( $value['url'] ) ) ? '' : $value;
		case 'gallery':
		case 'relationship':
		case 'checkbox':
			return is_array( $value ) ? array_map( 'strval', $value ) : fake_meta( $value );
		case 'post_object':
		case 'user':
		case 'select':
		case 'taxonomy':
		case 'page_link':
			return is_array( $value ) ? array_map( 'strval', $value ) : fake_meta( $value );
	}
	return fake_meta( $value );
}

function round_trips( array $field, $raw, $expected ) {
	$what       = $field['type'] . ' ' . var_export( $raw, true );
	$normalized = istota_fields_normalize( $field, $raw );
	same( $expected, $normalized, "normalize $what" );
	$again = istota_fields_normalize( $field, fake_store( $field, istota_fields_denormalize( $field, $normalized ) ) );
	same( $normalized, $again, "round trip $what" );
}

// ---------------------------------------------------------------------------
// The value model.

test(
	'every scalar type round-trips',
	function () {
		$strings = array( 'text', 'textarea', 'wysiwyg', 'email', 'url', 'password', 'oembed', 'color_picker', 'date_picker', 'date_time_picker', 'time_picker', 'radio', 'button_group' );
		foreach ( $strings as $type ) {
			$field = f( $type, 'x' );
			round_trips( $field, 'hello', 'hello' );
			round_trips( $field, '0', '0' );
			round_trips( $field, '', null );
			round_trips( $field, null, null );
			round_trips( $field, false, null );
			round_trips( $field, 0, '0' );
		}
		round_trips( f( 'date_picker', 'd' ), '20261001', '20261001' );
		round_trips( f( 'date_time_picker', 'd' ), '2026-10-01 09:30:00', '2026-10-01 09:30:00' );

		$single = f( 'select', 's', array( 'multiple' => 0 ) );
		round_trips( $single, 'sm', 'sm' );
		round_trips( $single, array( 'lg' ), 'lg' );
		round_trips( $single, '', null );

		foreach ( array( 'number', 'range' ) as $type ) {
			$field = f( $type, 'n' );
			round_trips( $field, '5', 5 );
			round_trips( $field, '0', 0 );
			round_trips( $field, 0, 0 );
			round_trips( $field, '2.5', 2.5 );
			round_trips( $field, '3.0', 3 );
			round_trips( $field, '', null );
			round_trips( $field, null, null );
			round_trips( $field, false, null );
		}

		$bool = f( 'true_false', 'b' );
		round_trips( $bool, '1', true );
		round_trips( $bool, '0', false );
		round_trips( $bool, 1, true );
		round_trips( $bool, 0, false );
		round_trips( $bool, true, true );
		round_trips( $bool, false, false );
		round_trips( $bool, '', null );
		round_trips( $bool, null, null );

		foreach ( array( 'image', 'file' ) as $type ) {
			$field = f( $type, 'i' );
			round_trips( $field, '12', 12 );
			round_trips( $field, 12, 12 );
			round_trips( $field, '', null );
			round_trips( $field, 0, null );
			round_trips( $field, '0', null );
			round_trips( $field, false, null );
			round_trips( $field, null, null );
		}

		$gallery = f( 'gallery', 'g' );
		round_trips( $gallery, array( '3', '4' ), array( 3, 4 ) );
		round_trips( $gallery, '', array() );
		round_trips( $gallery, false, array() );
		round_trips( $gallery, null, array() );

		$relationship = f( 'relationship', 'r' );
		round_trips( $relationship, array( '7', '9' ), array( 7, 9 ) );
		round_trips( $relationship, '', array() );
		round_trips( $relationship, null, array() );

		$post = f( 'post_object', 'p', array( 'multiple' => 0 ) );
		round_trips( $post, '42', 42 );
		round_trips( $post, '', null );
		round_trips( $post, false, null );
		round_trips( $post, null, null );
		$posts = f( 'post_object', 'p', array( 'multiple' => 1 ) );
		round_trips( $posts, array( '1', '2' ), array( 1, 2 ) );
		round_trips( $posts, '', array() );
		round_trips( $posts, null, array() );

		$user = f( 'user', 'u', array( 'multiple' => 0 ) );
		round_trips( $user, '5', 5 );
		round_trips( $user, '', null );

		$page_link = f( 'page_link', 'pl', array( 'multiple' => 0 ) );
		round_trips( $page_link, '8', 8 );
		round_trips( $page_link, 'https://example.test/x', 'https://example.test/x' );
		round_trips( $page_link, '', null );

		$term = f( 'taxonomy', 't', array( 'field_type' => 'select' ) );
		round_trips( $term, '11', 11 );
		round_trips( $term, null, null );
		$terms = f( 'taxonomy', 't', array( 'field_type' => 'checkbox' ) );
		round_trips( $terms, array( '11', '12' ), array( 11, 12 ) );
		round_trips( $terms, false, array() );
		$multi_terms = f( 'taxonomy', 't', array( 'field_type' => 'multi_select' ) );
		round_trips( $multi_terms, array( 13 ), array( 13 ) );

		$checkbox = f( 'checkbox', 'c' );
		round_trips( $checkbox, array( 'a', 'b' ), array( 'a', 'b' ) );
		round_trips( $checkbox, 'a', array( 'a' ) );
		round_trips( $checkbox, '', array() );
		round_trips( $checkbox, null, array() );
		$multi = f( 'select', 'm', array( 'multiple' => 1 ) );
		round_trips( $multi, array( 'sm', 'lg' ), array( 'sm', 'lg' ) );
		round_trips( $multi, array(), array() );

		$link = f( 'link', 'l', array( 'return_format' => 'array' ) );
		$stored = array(
			'title'  => 'Home',
			'url'    => 'https://example.test/',
			'target' => '_blank',
		);
		round_trips(
			$link,
			$stored,
			array(
				'url'    => 'https://example.test/',
				'title'  => 'Home',
				'target' => '_blank',
			)
		);
		round_trips( $link, '', null );
		round_trips( $link, null, null );
		round_trips( $link, array( 'url' => '' ), null );
		$link_url = f( 'link', 'l', array( 'return_format' => 'url' ) );
		round_trips( $link_url, $stored, 'https://example.test/' );
		round_trips( $link_url, '', null );

		$edges = array( 'gallery' => array(), 'relationship' => array(), 'checkbox' => array() );
		foreach ( $edges as $type => $empty ) {
			foreach ( array( false, '', null ) as $raw ) {
				round_trips( f( $type, 'e' ), $raw, $empty );
			}
		}
		round_trips( f( 'gallery', 'e' ), '0', array() );
		round_trips( f( 'gallery', 'e' ), 0, array() );
		round_trips( f( 'relationship', 'e' ), '0', array() );
		round_trips( f( 'relationship', 'e' ), 0, array() );
		round_trips( f( 'checkbox', 'e' ), '0', array( '0' ) );
		foreach ( array( 'user', 'page_link', 'post_object' ) as $type ) {
			foreach ( array( false, '', null, '0', 0 ) as $raw ) {
				round_trips( f( $type, 'e', array( 'multiple' => 0 ) ), $raw, null );
			}
		}
		round_trips( f( 'link', 'e' ), false, null );
		// ACF's link update_value stores a url empty() calls empty as "", "0" included.
		round_trips( f( 'link', 'e' ), '0', null );

		$map = f( 'google_map', 'gm' );
		$place = array(
			'address' => 'Somewhere',
			'lat'     => 52.1,
			'lng'     => 4.3,
		);
		round_trips( $map, $place, $place );
		round_trips( $map, false, null );
		round_trips( $map, '', null );
		round_trips( $map, null, null );
	}
);

test(
	'containers round-trip, keyed by name, every sub-field present',
	function () {
		$blocks = blocks_value();
		same( array( 'acf_fc_layout', 'title', 'items', 'options' ), array_keys( $blocks[0] ), 'a flexible row carries its layout then every valued sub-field, tab and message skipped' );
		same(
			array(
				'background' => '#fff',
				'spacing'    => 'sm',
			),
			$blocks[0]['options'],
			'a spliced seamless clone reads by its composite key'
		);
		same(
			array(
				'label' => 'a',
				'link'  => null,
				'done'  => false,
			),
			$blocks[0]['items'][0],
			'repeater rows: name keys, "0" true_false is false, "" link is null'
		);
		same(
			array(
				'acf_fc_layout' => 'text',
				'body'          => '<p>Hi</p>',
				'image'         => null,
			),
			$blocks[1],
			'an empty image is null'
		);

		// A row stored with nothing for some sub-fields still carries all of them.
		$sparse = istota_fields_normalize( blocks_field(), array( array( 'acf_fc_layout' => 'list' ) ) );
		same(
			array(
				'acf_fc_layout' => 'list',
				'title'         => null,
				'items'         => array(),
				'options'       => array(
					'background' => null,
					'spacing'    => null,
				),
			),
			$sparse[0],
			'every defined sub-field is present as null'
		);

		$again = istota_fields_normalize( blocks_field(), fake_store( blocks_field(), istota_fields_denormalize( blocks_field(), $blocks ) ) );
		same( $blocks, $again, 'flexible content round-trips' );
		round_trips( blocks_field(), '', array() );
		round_trips( blocks_field(), null, array() );
		round_trips( people_field(), false, array() );
		round_trips( people_field(), array( array( 'field_name' => 'Ann', 'field_age' => '30' ) ), array( array( 'name' => 'Ann', 'age' => 30 ) ) );
		round_trips( options_group(), null, array( 'background' => null, 'spacing' => null ) );

		$clone = f(
			'clone',
			'typography',
			array(
				'display'    => 'group',
				'sub_fields' => array(
					array( 'key' => 'field_typography_field_size', 'name' => 'size', 'type' => 'number', 'label' => 'Size', 'required' => 0 ),
				),
			)
		);
		round_trips( $clone, array( 'field_typography_field_size' => '14' ), array( 'size' => 14 ) );
		round_trips( $clone, null, array( 'size' => null ) );
	}
);

test(
	'denormalize writes keys and clears nulls a group would skip',
	function () {
		$row = istota_fields_denormalize( blocks_field(), array( array( 'acf_fc_layout' => 'list', 'title' => 'T', 'items' => array(), 'options' => array( 'background' => null, 'spacing' => 'lg' ) ) ) );
		same(
			array(
				'acf_fc_layout'                          => 'list',
				'field_title'                            => 'T',
				'field_items'                            => array(),
				'field_64f0a1b2c3d4e_field_5f1e2d3c4b5a6' => array(
					'field_64f0a1b2c3d4e_field_bg' => '',
					'field_64f0a1b2c3d4e_field_sp' => 'lg',
				),
			),
			$row[0],
			'names map back to keys, a null leaf is written as "" so a group stores it'
		);
		same( 0, istota_fields_denormalize( f( 'true_false', 'b' ), false ), 'false is written as 0, which reads back as false' );
		same( 1, istota_fields_denormalize( f( 'true_false', 'b' ), true ), 'true is written as 1' );
		same( '', istota_fields_denormalize( f( 'true_false', 'b' ), null ), 'null is written as nothing' );

		// A row that omits a sub-field still writes it, or an insert above
		// would leave the shifted row's old meta behind.
		$rows = istota_fields_denormalize( people_field(), array( array( 'name' => 'Ann' ) ) );
		same( array( array( 'field_name' => 'Ann', 'field_age' => '' ) ), $rows, 'a sub-field a row omits is written empty' );
	}
);

test(
	'seamless clones and third-party types',
	function () {
		$seamless = f(
			'clone',
			'sidebar',
			array(
				'display'    => 'seamless',
				'sub_fields' => array( f( 'text', 'x' ) ),
			)
		);
		$e = raised(
			function () use ( $seamless ) {
				istota_fields_normalize( $seamless, array() );
			}
		);
		ok( $e && 'unsupported_field' === $e->reason, 'a seamless clone ACF did not splice is unsupported_field' );
		$e = raised(
			function () use ( $seamless ) {
				istota_fields_denormalize( $seamless, array() );
			}
		);
		ok( $e && 'unsupported_field' === $e->reason, 'and cannot be written either' );

		$third = f( 'icon_set', 'icon' );
		$raw   = array(
			'set'  => 'fa',
			'name' => 'star',
		);
		same( $raw, istota_fields_normalize( $third, $raw ), 'a third-party value is passed through unchanged' );
		same( $raw, istota_fields_denormalize( $third, $raw ), 'and written back unchanged' );
		$definition = istota_fields_definition( $third );
		same( true, $definition['raw'], 'its definition is marked raw' );
		$e = raised(
			function () use ( $third, $raw ) {
				istota_fields_apply( $third, $raw, array( array( 'op' => 'set', 'path' => 'icon/name', 'value' => 'x' ) ) );
			}
		);
		ok( $e && 'unknown_path' === $e->reason && 'icon' === $e->params['exists'], 'a raw value cannot be addressed into' );
		$result = apply_ok( $third, $raw, array( array( 'op' => 'set', 'path' => 'icon', 'value' => array( 'any' => 'thing' ) ) ) );
		same( array( 'any' => 'thing' ), $result['value'], 'but can be set whole' );
	}
);

test(
	'definitions',
	function () {
		$definition = istota_fields_definition( blocks_field() );
		same( 'flexible_content', $definition['type'], 'type' );
		same( 5, $definition['max'], 'max' );
		ok( ! isset( $definition['min'] ), 'an empty min is left out' );
		same( array( 'list', 'text', 'hero' ), array_column( $definition['layouts'], 'name' ), 'layouts by name' );
		same( array( 'title', 'items', 'options' ), array_column( $definition['layouts'][0]['sub_fields'], 'name' ), 'layout-only fields are not in a definition' );
		same( 2, $definition['layouts'][1]['max'], 'a layout max' );
		$spacing = $definition['layouts'][0]['sub_fields'][2]['sub_fields'][1];
		same( array( 'sm' => 'Small', 'lg' => 'Large' ), $spacing['choices'], 'choices' );
		same( false, $spacing['multiple'], 'multiple' );
		same( true, istota_fields_definition( items_field() )['sub_fields'][0]['required'], 'required' );
	}
);

// ---------------------------------------------------------------------------
// Paths.

test(
	'paths parse and refuse',
	function () {
		same( array( 'blocks', 0, 'items', 3, 'label' ), istota_fields_parse_path( 'blocks/0/items/3/label' ), 'names and indices' );
		same( array( 'blocks', 10 ), istota_fields_parse_path( 'blocks/10' ), 'a multi-digit index' );
		same( array( 'blocks', '-' ), istota_fields_parse_path( 'blocks/-', true ), '"-" last on an insert' );
		same( array( 'a-b_C9' ), istota_fields_parse_path( 'a-b_C9' ), 'a name with dashes and underscores' );

		$bad = array( '', '/blocks', 'blocks/', 'blocks//0', 'blocks/01', 'blocks/0.5','blocks/a b', 'blocks/é', '0/blocks', "blocks\n", 'blocks/1234567890', str_repeat( 'a', 65 ) );
		foreach ( $bad as $path ) {
			$e = raised(
				function () use ( $path ) {
					istota_fields_parse_path( $path, true );
				}
			);
			ok( $e && 'validation_error' === $e->reason, 'refused: ' . json_encode( $path ) );
		}
		$e = raised(
			function () {
				istota_fields_parse_path( 'blocks/-' );
			}
		);
		ok( $e && 'validation_error' === $e->reason, '"-" refused outside an insert' );
		$e = raised(
			function () {
				istota_fields_parse_path( 'blocks/-/items', true );
			}
		);
		ok( $e && 'validation_error' === $e->reason, '"-" refused before the last segment' );
		$e = raised(
			function () {
				istota_fields_parse_path( 'a/b/c/d/e/f/g/h/i/j/k/l/m/n/o/p/q' );
			}
		);
		ok( $e && 'validation_error' === $e->reason, '17 segments refused' );
		same( 16, count( istota_fields_parse_path( 'a/b/c/d/e/f/g/h/i/j/k/l/m/n/o/p' ) ), '16 segments allowed' );
		$e = raised(
			function () {
				istota_fields_parse_path( 12 );
			}
		);
		ok( $e && 'validation_error' === $e->reason, 'a non-string path refused' );
	}
);

test(
	'resolve finds a node or names the deepest prefix',
	function () {
		$blocks = blocks_value();
		$found  = istota_fields_resolve( blocks_field(), $blocks, istota_fields_parse_path( 'blocks/0/items/2/label' ) );
		same( true, $found['found'], 'found' );
		same( 'c', $found['value'], 'the node' );
		same( 'text', $found['field']['type'], 'its definition' );

		$row = istota_fields_resolve( blocks_field(), $blocks, istota_fields_parse_path( 'blocks/1' ) );
		same( 'row', $row['field']['type'], 'a row resolves to a row definition' );
		same( 'text', $row['field']['layout'], 'with its layout' );

		$cases = array(
			'blocks/7'                 => 'blocks',
			'blocks/0/items/3'         => 'blocks/0/items',
			'blocks/0/nope'            => 'blocks/0',
			'blocks/0/acf_fc_layout'   => 'blocks/0',
			'blocks/title'             => 'blocks',
			'blocks/0/title/0'         => 'blocks/0/title',
			'blocks/0/options/0'       => 'blocks/0/options',
			'blocks/1/items'           => 'blocks/1',
			'other'                    => '',
		);
		foreach ( $cases as $path => $exists ) {
			$missing = istota_fields_resolve( blocks_field(), $blocks, istota_fields_parse_path( $path ) );
			same( array( 'found' => false, 'exists' => $exists ), $missing, "unknown $path" );
		}
	}
);

// ---------------------------------------------------------------------------
// Operations.

test(
	'insert, remove and move on flexible content and repeaters',
	function () {
		$field  = blocks_field();
		$blocks = blocks_value();
		$item   = array( 'label' => 'new' );

		$at0 = apply_ok( $field, $blocks, array( array( 'op' => 'insert', 'path' => 'blocks/0/items/0', 'value' => $item ) ) );
		same( array( 'new', 'a', 'b', 'c' ), labels( $at0['value'], 0 ), 'insert at 0' );
		same( array( 'label' => 'new', 'link' => null, 'done' => null ), $at0['value'][0]['items'][0], 'an inserted row carries every sub-field' );

		$at_end = apply_ok( $field, $blocks, array( array( 'op' => 'insert', 'path' => 'blocks/0/items/3', 'value' => $item ) ) );
		same( array( 'a', 'b', 'c', 'new' ), labels( $at_end['value'], 0 ), 'insert at the length' );

		$append = apply_ok( $field, $blocks, array( array( 'op' => 'insert', 'path' => 'blocks/2/items/-', 'value' => $item ) ) );
		same( array( 'x', 'new' ), labels( $append['value'], 2 ), 'insert at "-"' );
		same( 'blocks/2/items/1', $append['written'][0]['path'], 'the written path names the index "-" became' );

		$e = raised(
			function () use ( $field, $blocks, $item ) {
				istota_fields_apply( $field, $blocks, array( array( 'op' => 'insert', 'path' => 'blocks/0/items/5', 'value' => $item ) ) );
			}
		);
		ok( $e && 'unknown_path' === $e->reason && 'ops[0]' === $e->params['op'], 'insert past the length is unknown_path, naming the op' );

		$forward = apply_ok( $field, $blocks, array( array( 'op' => 'move', 'from' => 'blocks/0/items/0', 'path' => 'blocks/0/items/2' ) ) );
		same( array( 'b', 'c', 'a' ), labels( $forward['value'], 0 ), 'move forward ends at the destination index' );
		$backward = apply_ok( $field, $blocks, array( array( 'op' => 'move', 'from' => 'blocks/0/items/2', 'path' => 'blocks/0/items/0' ) ) );
		same( array( 'c', 'a', 'b' ), labels( $backward['value'], 0 ), 'move backward' );
		$rows = apply_ok( $field, $blocks, array( array( 'op' => 'move', 'from' => 'blocks/2', 'path' => 'blocks/0' ) ) );
		same( array( 'Second', 'First', 'text' ), titles( $rows['value'] ), 'move a flexible content row' );

		$e = raised(
			function () use ( $field, $blocks ) {
				istota_fields_apply( $field, $blocks, array( array( 'op' => 'move', 'from' => 'blocks/0/items/0', 'path' => 'blocks/2/items/0' ) ) );
			}
		);
		ok( $e && 'validation_error' === $e->reason, 'a move between two lists is refused' );
		$e = raised(
			function () use ( $field, $blocks ) {
				istota_fields_apply( $field, $blocks, array( array( 'op' => 'move', 'from' => 'blocks/0/items/0', 'path' => 'blocks/0/items/3' ) ) );
			}
		);
		ok( $e && 'unknown_path' === $e->reason, 'a move destination is an index of the list before the move' );

		$last = apply_ok( $field, $blocks, array( array( 'op' => 'remove', 'path' => 'blocks/0/items/2' ) ) );
		same( array( 'a', 'b' ), labels( $last['value'], 0 ), 'remove the last row' );
		same( array( 'label' => 'c', 'link' => null, 'done' => false ), $last['previous'][0], 'previous holds the removed row' );
		$block = apply_ok( $field, $blocks, array( array( 'op' => 'remove', 'path' => 'blocks/1' ) ) );
		same( array( 'First', 'Second' ), titles( $block['value'] ), 'remove a flexible content row' );

		$e = raised(
			function () use ( $field, $blocks ) {
				istota_fields_apply( $field, $blocks, array( array( 'op' => 'remove', 'path' => 'blocks/0/title' ) ) );
			}
		);
		ok( $e && 'validation_error' === $e->reason, 'remove acts only on rows' );
		foreach ( array( 'insert', 'remove', 'move' ) as $verb ) {
			$e = raised(
				function () use ( $field, $blocks, $verb ) {
					istota_fields_apply( $field, $blocks, array( array( 'op' => $verb, 'path' => 'blocks', 'from' => 'blocks', 'value' => array() ) ) );
				}
			);
			ok( $e && 'validation_error' === $e->reason, "$verb of a whole field is malformed, not an unknown path" );
		}

		// The same op matrix on flexible content rows.
		$hero = array( 'acf_fc_layout' => 'hero', 'image' => 9 );
		same( array( 'hero', 'First', 'text', 'Second' ), titles( apply_ok( $field, $blocks, array( array( 'op' => 'insert', 'path' => 'blocks/0', 'value' => $hero ) ) )['value'] ), 'flexible: insert at 0' );
		same( array( 'First', 'text', 'Second', 'hero' ), titles( apply_ok( $field, $blocks, array( array( 'op' => 'insert', 'path' => 'blocks/3', 'value' => $hero ) ) )['value'] ), 'flexible: insert at the length' );
		same( array( 'First', 'text', 'Second', 'hero' ), titles( apply_ok( $field, $blocks, array( array( 'op' => 'insert', 'path' => 'blocks/-', 'value' => $hero ) ) )['value'] ), 'flexible: insert at "-"' );
		same( array( 'text', 'Second', 'First' ), titles( apply_ok( $field, $blocks, array( array( 'op' => 'move', 'from' => 'blocks/0', 'path' => 'blocks/2' ) ) )['value'] ), 'flexible: move forward' );
		same( array( 'First', 'text' ), titles( apply_ok( $field, $blocks, array( array( 'op' => 'remove', 'path' => 'blocks/2' ) ) )['value'] ), 'flexible: remove the last row' );
		$e = raised(
			function () use ( $field, $blocks ) {
				istota_fields_apply( $field, $blocks, array( array( 'op' => 'insert', 'path' => 'blocks/0/options/0', 'value' => array() ) ) );
			}
		);
		ok( $e && 'validation_error' === $e->reason, 'insert acts only on repeater and flexible content lists' );

		$flex = apply_ok( $field, $blocks, array( array( 'op' => 'insert', 'path' => 'blocks/1', 'value' => array( 'acf_fc_layout' => 'hero', 'image' => 5 ) ) ) );
		same( array( 'acf_fc_layout' => 'hero', 'image' => 5 ), $flex['value'][1], 'insert a flexible content row' );

		$people = apply_ok( people_field(), people( array( 'Ann', 'Bo' ) ), array( array( 'op' => 'insert', 'path' => 'people/1', 'value' => array( 'name' => 'Cy', 'age' => 3 ) ) ) );
		same( people( array( 'Ann', 'Cy', 'Bo' ) )[0], $people['value'][0], 'a top-level repeater insert' );
		same( 3, $people['value'][1]['age'], 'the inserted row is in place' );
	}
);

test(
	'set replaces a node',
	function () {
		$field  = blocks_field();
		$blocks = blocks_value();

		$label = apply_ok( $field, $blocks, array( array( 'op' => 'set', 'path' => 'blocks/0/items/1/label', 'value' => 'B' ) ) );
		same( array( 'a', 'B', 'c' ), labels( $label['value'], 0 ), 'set a leaf' );
		same( 'b', $label['previous'][0], 'previous is the replaced node' );
		same( 'text', $label['written'][0]['field']['type'], 'written carries the definition' );

		$group = apply_ok( $field, $blocks, array( array( 'op' => 'set', 'path' => 'blocks/0/options', 'value' => array( 'spacing' => 'lg' ) ) ) );
		same( array( 'background' => null, 'spacing' => 'lg' ), $group['value'][0]['options'], 'a group set whole: what it leaves out is cleared' );

		$row = apply_ok( $field, $blocks, array( array( 'op' => 'set', 'path' => 'blocks/1', 'value' => array( 'body' => 'new' ) ) ) );
		same( array( 'acf_fc_layout' => 'text', 'body' => 'new', 'image' => null ), $row['value'][1], 'a flexible row set without a layout keeps its own' );
		same( $blocks[1], $row['previous'][0], 'previous for a row set is the row as read' );
		$list = apply_ok( $field, $blocks, array( array( 'op' => 'set', 'path' => 'blocks', 'value' => array() ) ) );
		same( $blocks, $list['previous'][0], 'previous for a whole list is the list as read' );

		$changed = istota_fields_apply( $field, $blocks, array( array( 'op' => 'set', 'path' => 'blocks/1', 'value' => array( 'acf_fc_layout' => 'hero', 'image' => 4 ) ) ) );
		ok( in_array( 'blocks/1/acf_fc_layout', array_column( $changed['errors'], 'path' ), true ), 'changing a row\'s layout by set is refused' );

		$cleared = istota_fields_apply( $field, $blocks, array( array( 'op' => 'set', 'path' => 'blocks/0/items/0', 'value' => array( 'done' => true ) ) ) );
		same( array( 'blocks/0/items/0/label' ), array_column( $cleared['errors'], 'path' ), 'a set that leaves out a required sub-field is refused' );

		// A shape-invalid set followed by an op inside it is an unknown path, never a PHP error.
		$e = raised(
			function () use ( $field, $blocks ) {
				istota_fields_apply(
					$field,
					$blocks,
					array(
						array( 'op' => 'set', 'path' => 'blocks/0/options', 'value' => 5 ),
						array( 'op' => 'set', 'path' => 'blocks/0/options/spacing', 'value' => 'lg' ),
					)
				);
			}
		);
		ok( $e && 'unknown_path' === $e->reason && 'ops[1]' === $e->params['op'], 'an op into a value a set left malformed is unknown_path' );

		$whole = apply_ok( f( 'text', 'subtitle' ), 'old', array( array( 'op' => 'set', 'path' => 'subtitle', 'value' => 'new' ) ) );
		same( 'new', $whole['value'], 'a whole top-level field' );

		$e = raised(
			function () use ( $field, $blocks ) {
				istota_fields_apply( $field, $blocks, array( array( 'op' => 'set', 'path' => 'blocks/0/acf_fc_layout', 'value' => 'text' ) ) );
			}
		);
		ok( $e && 'unknown_path' === $e->reason && 'blocks/0' === $e->params['exists'], 'acf_fc_layout is not addressable' );
		$e = raised(
			function () use ( $field, $blocks ) {
				istota_fields_apply( $field, $blocks, array( array( 'op' => 'set', 'path' => 'blocks/3', 'value' => array() ) ) );
			}
		);
		ok( $e && 'unknown_path' === $e->reason, 'set needs an existing node' );
	}
);

test(
	'ops apply in sequence, each seeing the indices the last one left',
	function () {
		$field  = blocks_field();
		$result = apply_ok(
			$field,
			blocks_value(),
			array(
				array( 'op' => 'insert', 'path' => 'blocks/0/items/0', 'value' => array( 'label' => 'z' ) ),
				array( 'op' => 'set', 'path' => 'blocks/0/items/1/label', 'value' => 'A' ),
				array( 'op' => 'remove', 'path' => 'blocks/0/items/3' ),
				array( 'op' => 'move', 'from' => 'blocks/0/items/0', 'path' => 'blocks/0/items/2' ),
				array( 'op' => 'remove', 'path' => 'blocks/1' ),
				array( 'op' => 'set', 'path' => 'blocks/1/title', 'value' => 'Was third' ),
			)
		);
		same( array( 'A', 'b', 'z' ), labels( $result['value'], 0 ), 'insert, set and remove see the shifted indices' );
		same( array( 'First', 'Was third' ), titles( $result['value'] ), 'a removed block shifts the next one up' );
		same( 'a', $result['previous'][1], 'previous is the node before that op' );
		same( 'c', $result['previous'][2]['label'], 'the removed row is the one the shifted index named' );
		same( array( null, 'a', $result['previous'][2], null, $result['previous'][4], 'Second' ), $result['previous'], 'previous for set and remove only' );

		$e = raised(
			function () use ( $field ) {
				istota_fields_apply(
					$field,
					blocks_value(),
					array(
						array( 'op' => 'remove', 'path' => 'blocks/2' ),
						array( 'op' => 'set', 'path' => 'blocks/2/title', 'value' => 'x' ),
					)
				);
			}
		);
		ok( $e && 'unknown_path' === $e->reason && 'ops[1]' === $e->params['op'], 'a later op sees the earlier remove' );
	}
);

test(
	'operations are refused whole',
	function () {
		$field = blocks_field();
		$cases = array(
			'mixed_fields'     => array(
				array( 'op' => 'set', 'path' => 'blocks/0/title', 'value' => 'x' ),
				array( 'op' => 'set', 'path' => 'subtitle', 'value' => 'x' ),
			),
			'mixed move'       => array( array( 'op' => 'move', 'from' => 'other/0', 'path' => 'blocks/0' ) ),
			'no op'            => array( array( 'path' => 'blocks/0' ) ),
			'unknown op'       => array( array( 'op' => 'copy', 'path' => 'blocks/0' ) ),
			'set needs value'  => array( array( 'op' => 'set', 'path' => 'blocks/0/title' ) ),
			'move needs from'  => array( array( 'op' => 'move', 'path' => 'blocks/0' ) ),
			'empty'            => array(),
			'too many'         => array_fill( 0, 51, array( 'op' => 'remove', 'path' => 'blocks/0' ) ),
		);
		foreach ( $cases as $what => $ops ) {
			$e = raised(
				function () use ( $field, $ops ) {
					istota_fields_apply( $field, blocks_value(), $ops );
				}
			);
			$reason = 'mixed_fields' === $what || 'mixed move' === $what ? 'mixed_fields' : 'validation_error';
			ok( $e && $reason === $e->reason, "$what is $reason" );
		}
		// The mixed check runs before any op applies: the first op is fine.
		$e = raised(
			function () use ( $field, $cases ) {
				istota_fields_apply( $field, blocks_value(), $cases['mixed_fields'] );
			}
		);
		same( 'ops[1]', $e->params['op'], 'mixed_fields names the op' );
	}
);

test(
	'shape is checked on what the ops write, and only that',
	function () {
		$field  = blocks_field();
		$result = istota_fields_apply(
			$field,
			blocks_value(),
			array(
				array( 'op' => 'set', 'path' => 'blocks/0/items/0/done', 'value' => 'yes' ),
				array( 'op' => 'set', 'path' => 'blocks/1/image', 'value' => '12' ),
				array( 'op' => 'insert', 'path' => 'blocks/0/items/0', 'value' => array( 'label' => 'x', 'colour' => 'red' ) ),
				array( 'op' => 'set', 'path' => 'blocks/0/items/1/label', 'value' => '' ),
				array( 'op' => 'insert', 'path' => 'blocks/-', 'value' => array( 'acf_fc_layout' => 'nope' ) ),
				array( 'op' => 'set', 'path' => 'blocks/0/options/spacing', 'value' => array( 'sm' ) ),
				array( 'op' => 'set', 'path' => 'blocks/0/title', 'value' => null ),
			)
		);
		$paths = array();
		foreach ( $result['errors'] as $error ) {
			$paths[] = $error['op'] . ' ' . $error['path'];
		}
		same(
			array(
				'ops[0] blocks/0/items/0/done',
				'ops[1] blocks/1/image',
				'ops[2] blocks/0/items/0/colour',
				'ops[3] blocks/0/items/1/label',
				'ops[4] blocks/3/acf_fc_layout',
				'ops[5] blocks/0/options/spacing',
			),
			$paths,
			'every shape error is collected, keyed by op and path; a non-required null is fine'
		);

		// The stored rows were never valid (a required body is empty) and are not checked.
		$value      = blocks_value();
		$value[1]['body'] = null;
		apply_ok( $field, $value, array( array( 'op' => 'set', 'path' => 'blocks/0/title', 'value' => 'ok' ) ) );

		$good = apply_ok(
			$field,
			blocks_value(),
			array(
				array( 'op' => 'set', 'path' => 'blocks/0/items/0/done', 'value' => true ),
				array( 'op' => 'set', 'path' => 'blocks/0/items/0/link', 'value' => array( 'url' => 'https://example.test/', 'title' => 'T', 'target' => '' ) ),
				array( 'op' => 'set', 'path' => 'blocks/1/image', 'value' => null ),
				array( 'op' => 'set', 'path' => 'blocks/0/options/spacing', 'value' => 'lg' ),
			)
		);
		same( true, $good['value'][0]['items'][0]['done'], 'well-shaped values apply' );
	}
);

test(
	'a required sub-field an insert leaves out is reported, not refused',
	function () {
		$result = apply_ok(
			blocks_field(),
			blocks_value(),
			array(
				array( 'op' => 'insert', 'path' => 'blocks/0/items/-', 'value' => array( 'done' => true ) ),
				array( 'op' => 'insert', 'path' => 'blocks/0', 'value' => array( 'acf_fc_layout' => 'text' ) ),
			)
		);
		same( array( 'blocks/0/items/3/label', 'blocks/0/body' ), $result['missing_required'], 'missing_required names the paths' );
	}
);

test(
	'row counts: min and max, and lists already out of range',
	function () {
		$field = people_field();
		$full  = people( array( 'a', 'b', 'c' ) );
		$over  = istota_fields_apply( $field, $full, array( array( 'op' => 'insert', 'path' => 'people/-', 'value' => array() ) ) );
		ok( 1 === count( $over['errors'] ) && 'people' === $over['errors'][0]['path'], 'an insert above max is refused' );

		$one   = people( array( 'a' ) );
		$under = istota_fields_apply( $field, $one, array( array( 'op' => 'remove', 'path' => 'people/0' ) ) );
		ok( 1 === count( $under['errors'] ), 'a remove below min is refused' );

		apply_ok(
			$field,
			$full,
			array(
				array( 'op' => 'insert', 'path' => 'people/0', 'value' => array( 'name' => 'new' ) ),
				array( 'op' => 'remove', 'path' => 'people/3' ),
			)
		);

		$five = people( array( 'a', 'b', 'c', 'd', 'e' ) );
		apply_ok( $field, $five, array( array( 'op' => 'remove', 'path' => 'people/0' ) ) );
		apply_ok( $field, $five, array( array( 'op' => 'set', 'path' => 'people/0/name', 'value' => 'A' ) ) );
		$worse = istota_fields_apply( $field, $five, array( array( 'op' => 'insert', 'path' => 'people/0', 'value' => array() ) ) );
		ok( 1 === count( $worse['errors'] ), 'a list already over max may not grow' );
		$empty = istota_fields_apply( $field, array(), array( array( 'op' => 'set', 'path' => 'people', 'value' => array() ) ) );
		same( array(), $empty['errors'], 'a list already under min may stay there' );

		// A layout's own max within flexible content.
		$blocks = blocks_value();
		$texts  = apply_ok( blocks_field(), $blocks, array( array( 'op' => 'insert', 'path' => 'blocks/0', 'value' => array( 'acf_fc_layout' => 'text', 'body' => 'x' ) ) ) );
		$third  = istota_fields_apply( blocks_field(), $texts['value'], array( array( 'op' => 'insert', 'path' => 'blocks/0', 'value' => array( 'acf_fc_layout' => 'text', 'body' => 'y' ) ) ) );
		ok( 1 === count( $third['errors'] ) && false !== strpos( $third['errors'][0]['message'], 'text' ), 'a layout above its max is refused' );

		// Nested lists keep their identity when their row moves.
		$items = items_field();
		$four  = array_fill( 0, 5, array( 'label' => 'i' ) );
		$moved = istota_fields_normalize( blocks_field(), raw_blocks() );
		$moved[0]['items'] = istota_fields_normalize( $items, array_fill( 0, 5, array( 'field_label' => 'i' ) ) );
		apply_ok(
			blocks_field(),
			$moved,
			array(
				array( 'op' => 'move', 'from' => 'blocks/0', 'path' => 'blocks/2' ),
				array( 'op' => 'remove', 'path' => 'blocks/2/items/0' ),
			)
		);
		$grown = istota_fields_apply( blocks_field(), $moved, array( array( 'op' => 'move', 'from' => 'blocks/2', 'path' => 'blocks/0' ) ) );
		same( array(), $grown['errors'], 'an over-max list another row\'s move shifted is not refused' );
		$whole = $moved[0];
		$whole['title'] = 'Renamed';
		$kept = istota_fields_apply( blocks_field(), $moved, array( array( 'op' => 'set', 'path' => 'blocks/0', 'value' => $whole ) ) );
		same( array(), $kept['errors'], 'a whole-row set keeps the row\'s identity: its over-max list is not made worse' );
		$add = istota_fields_apply( blocks_field(), $moved, array( array( 'op' => 'insert', 'path' => 'blocks/0', 'value' => array( 'acf_fc_layout' => 'list', 'items' => $four ) ) ) );
		ok( 1 === count( $add['errors'] ) && 'blocks/0/items' === $add['errors'][0]['path'], 'a new row with a nested list over max is refused' );

		// A nested list with a min that an inserted row leaves out is not held to it.
		$teams = f(
			'repeater',
			'teams',
			array(
				'sub_fields' => array(
					f( 'text', 'team' ),
					f( 'repeater', 'members', array( 'min' => 1, 'sub_fields' => array( f( 'text', 'who' ) ) ) ),
				),
			)
		);
		apply_ok( $teams, array(), array( array( 'op' => 'insert', 'path' => 'teams/0', 'value' => array( 'team' => 'A' ) ) ) );
		$short = istota_fields_apply(
			$teams,
			array( array( 'team' => 'A', 'members' => array( array( 'who' => 'x' ) ) ) ),
			array( array( 'op' => 'remove', 'path' => 'teams/0/members/0' ) )
		);
		ok( 1 === count( $short['errors'] ) && 'teams/0/members' === $short['errors'][0]['path'], 'but an existing one emptied below its min is refused' );
		$stored = array( array( 'team' => 'A', 'members' => array( array( 'who' => 'x' ) ) ) );
		$by_row = istota_fields_apply( $teams, $stored, array( array( 'op' => 'set', 'path' => 'teams/0', 'value' => array( 'team' => 'A', 'members' => array() ) ) ) );
		ok( 1 === count( $by_row['errors'] ) && 'teams/0/members' === $by_row['errors'][0]['path'], 'and so is one emptied by setting its whole row' );
	}
);

// ---------------------------------------------------------------------------
// The token.

test(
	'the token',
	function () {
		$a = array(
			'b' => 1,
			'a' => array( 'y' => 'é/x', 'x' => array( 2, 1 ) ),
		);
		$b = array(
			'a' => array( 'x' => array( 2, 1 ), 'y' => 'é/x' ),
			'b' => 1,
		);
		same( istota_fields_token( $a ), istota_fields_token( $b ), 'stable under key order' );
		same( '{"a":{"x":[2,1],"y":"é/x"},"b":1}', istota_fields_canonical_json( $a ), 'canonical JSON: sorted keys, unescaped slashes and unicode, list order kept' );
		same( 1, preg_match( '/^sha256:[0-9a-f]{64}$/D', istota_fields_token( $a ) ), 'sha256:, hex' );
		ok( istota_fields_token( array( 'x' => array( 1, 2 ) ) ) !== istota_fields_token( array( 'x' => array( 2, 1 ) ) ), 'list order counts' );

		$blocks = blocks_value();
		$before = istota_fields_token( $blocks );
		$edits  = array(
			array( 'op' => 'set', 'path' => 'blocks/0/items/0/done', 'value' => true ),
			array( 'op' => 'move', 'from' => 'blocks/0/items/0', 'path' => 'blocks/0/items/1' ),
			array( 'op' => 'insert', 'path' => 'blocks/0/items/-', 'value' => array() ),
			array( 'op' => 'remove', 'path' => 'blocks/1' ),
			array( 'op' => 'set', 'path' => 'blocks/1/image', 'value' => 3 ),
		);
		foreach ( $edits as $op ) {
			$result = istota_fields_apply( blocks_field(), $blocks, array( $op ) );
			ok( istota_fields_token( $result['value'] ) !== $before, 'changed by ' . $op['op'] . ' ' . $op['path'] );
		}
		$same = apply_ok( blocks_field(), $blocks, array( array( 'op' => 'set', 'path' => 'blocks/0/title', 'value' => 'First' ) ) );
		same( $before, istota_fields_token( $same['value'] ), 'a set to the same value leaves it' );
		same( $before, istota_fields_token( blocks_value() ), 'apply does not touch its input' );
		$e = raised(
			function () {
				istota_fields_token( array( 'n' => INF ) );
			}
		);
		ok( $e && 'unsupported_field' === $e->reason, 'a value JSON cannot encode is refused, not hashed as null' );
	}
);

$total = $GLOBALS['passes'] + $GLOBALS['failures'];
if ( $GLOBALS['failures'] ) {
	fwrite( STDERR, $GLOBALS['failures'] . " of $total checks failed\n" );
	exit( 1 );
}
echo "$total checks passed\n";
exit( 0 );
