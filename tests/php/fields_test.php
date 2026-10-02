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
// rows, an empty flexible content field is stored as "". A flexible row's
// disabled flag and custom label go to the field's layout meta (ACF 6.5),
// recorded only where empty() is false, and come back on the raw row in the
// shape stage 2's raw read gives them.

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
				if ( ! empty( $row['acf_fc_layout_disabled'] ) ) {
					$stored['acf_fc_layout_disabled'] = true;
				}
				if ( ! empty( $row['acf_fc_layout_custom_label'] ) ) {
					$stored['acf_fc_layout_custom_label'] = (string) $row['acf_fc_layout_custom_label'];
				}
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
		// The url return format is the same object: ACF stores all three either way.
		$link_url = f( 'link', 'l', array( 'return_format' => 'url' ) );
		round_trips( $link_url, $stored, array( 'url' => 'https://example.test/', 'title' => 'Home', 'target' => '_blank' ) );
		same( $stored['title'], istota_fields_denormalize( $link_url, istota_fields_normalize( $link_url, $stored ) )['title'], 'a url-format link keeps its stored title on write' );
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
		same( array( 'acf_fc_layout', 'acf_fc_layout_disabled', 'acf_fc_layout_custom_label', 'title', 'items', 'options' ), array_keys( $blocks[0] ), 'a flexible row carries its reserved keys then every valued sub-field, tab and message skipped' );
		same( false, $blocks[0]['acf_fc_layout_disabled'], 'a row with no layout meta is enabled' );
		same( null, $blocks[0]['acf_fc_layout_custom_label'], 'and unlabelled' );
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
				'acf_fc_layout'              => 'text',
				'acf_fc_layout_disabled'     => false,
				'acf_fc_layout_custom_label' => null,
				'body'                       => '<p>Hi</p>',
				'image'                      => null,
			),
			$blocks[1],
			'an empty image is null'
		);

		// A row stored with nothing for some sub-fields still carries all of them.
		$sparse = istota_fields_normalize( blocks_field(), array( array( 'acf_fc_layout' => 'list' ) ) );
		same(
			array(
				'acf_fc_layout'              => 'list',
				'acf_fc_layout_disabled'     => false,
				'acf_fc_layout_custom_label' => null,
				'title'                      => null,
				'items'                      => array(),
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

		// A disabled, renamed row as stage 2's raw read returns it.
		$raw                                  = raw_blocks();
		$raw[1]['acf_fc_layout_disabled']     = '1';
		$raw[1]['acf_fc_layout_custom_label'] = 'Intro';
		$raw[2]['acf_fc_layout_disabled']     = '0';
		$raw[2]['acf_fc_layout_custom_label'] = '';
		$flagged                              = istota_fields_normalize( blocks_field(), $raw );
		same( array( true, 'Intro' ), array( $flagged[1]['acf_fc_layout_disabled'], $flagged[1]['acf_fc_layout_custom_label'] ), 'a disabled, renamed row reads as true and its label' );
		same( array( false, null ), array( $flagged[2]['acf_fc_layout_disabled'], $flagged[2]['acf_fc_layout_custom_label'] ), '"0" and "" read as enabled and unlabelled' );
		$written = istota_fields_denormalize( blocks_field(), $flagged );
		same( array( true, 'Intro' ), array( $written[1]['acf_fc_layout_disabled'], $written[1]['acf_fc_layout_custom_label'] ), 'denormalize passes both to ACF under its own keys' );
		same( array( false, '' ), array( $written[2]['acf_fc_layout_disabled'], $written[2]['acf_fc_layout_custom_label'] ), 'an enabled, unlabelled row is written so ACF clears the meta' );
		same( $flagged, istota_fields_normalize( blocks_field(), fake_store( blocks_field(), $written ) ), 'a disabled, renamed row round-trips' );
		ok( istota_fields_token( $flagged ) !== istota_fields_token( blocks_value() ), 'the token covers the reserved keys' );
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
				'acf_fc_layout_disabled'                 => false,
				'acf_fc_layout_custom_label'             => '',
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
		$unlimited = istota_fields_definition( f( 'repeater', 'r', array( 'min' => 0, 'max' => '0', 'sub_fields' => array() ) ) );
		ok( ! isset( $unlimited['min'] ) && ! isset( $unlimited['max'] ), 'a row count of 0 is no limit and left out' );
		$number = istota_fields_definition( f( 'number', 'n', array( 'min' => 0, 'max' => 10 ) ) );
		same( 0, $number['min'], 'a number min of 0 is a bound and kept' );
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
		same( array( 'acf_fc_layout' => 'hero', 'acf_fc_layout_disabled' => false, 'acf_fc_layout_custom_label' => null, 'image' => 5 ), $flex['value'][1], 'insert a flexible content row: the reserved keys default to enabled and unlabelled' );
		$hidden = apply_ok( $field, $blocks, array( array( 'op' => 'insert', 'path' => 'blocks/0', 'value' => array( 'acf_fc_layout' => 'hero', 'acf_fc_layout_disabled' => true, 'acf_fc_layout_custom_label' => 'Draft', 'image' => 5 ) ) ) );
		same( array( true, 'Draft' ), array( $hidden['value'][0]['acf_fc_layout_disabled'], $hidden['value'][0]['acf_fc_layout_custom_label'] ), 'an insert may set both' );

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
		same( array( 'acf_fc_layout' => 'text', 'acf_fc_layout_disabled' => false, 'acf_fc_layout_custom_label' => null, 'body' => 'new', 'image' => null ), $row['value'][1], 'a flexible row set without a layout keeps its own' );

		// A whole-row set keeps the reserved keys it leaves out, and uses the ones it gives.
		$flagged                                  = $blocks;
		$flagged[1]['acf_fc_layout_disabled']     = true;
		$flagged[1]['acf_fc_layout_custom_label'] = 'Intro';
		$kept_flags = apply_ok( $field, $flagged, array( array( 'op' => 'set', 'path' => 'blocks/1', 'value' => array( 'body' => 'new' ) ) ) );
		same( array( true, 'Intro' ), array( $kept_flags['value'][1]['acf_fc_layout_disabled'], $kept_flags['value'][1]['acf_fc_layout_custom_label'] ), 'a whole-row set that omits the reserved keys keeps them' );
		$given = apply_ok( $field, $flagged, array( array( 'op' => 'set', 'path' => 'blocks/1', 'value' => array( 'acf_fc_layout_disabled' => false, 'body' => 'new' ) ) ) );
		same( array( false, 'Intro' ), array( $given['value'][1]['acf_fc_layout_disabled'], $given['value'][1]['acf_fc_layout_custom_label'] ), 'a given one is used' );
		$moved_flags = apply_ok( $field, $flagged, array( array( 'op' => 'move', 'from' => 'blocks/1', 'path' => 'blocks/2' ) ) );
		same( array( true, 'Intro' ), array( $moved_flags['value'][2]['acf_fc_layout_disabled'], $moved_flags['value'][2]['acf_fc_layout_custom_label'] ), 'they move with their row' );
		foreach ( array( 'acf_fc_layout_disabled', 'acf_fc_layout_custom_label' ) as $reserved ) {
			$e = raised(
				function () use ( $field, $blocks, $reserved ) {
					istota_fields_apply( $field, $blocks, array( array( 'op' => 'set', 'path' => 'blocks/1/' . $reserved, 'value' => true ) ) );
				}
			);
			ok( $e && 'unknown_path' === $e->reason && 'blocks/1' === $e->params['exists'], "$reserved is not addressable" );
		}
		$bad_flags = istota_fields_apply( $field, $blocks, array( array( 'op' => 'set', 'path' => 'blocks/1', 'value' => array( 'acf_fc_layout_disabled' => 'yes', 'acf_fc_layout_custom_label' => 3, 'body' => 'b' ) ) ) );
		same( array( 'blocks/1/acf_fc_layout_disabled', 'blocks/1/acf_fc_layout_custom_label' ), array_column( $bad_flags['errors'], 'path' ), 'the reserved keys are shape-checked when written' );
		$on_repeater = istota_fields_apply( $field, $blocks, array( array( 'op' => 'set', 'path' => 'blocks/0/items/0', 'value' => array( 'label' => 'x', 'acf_fc_layout_disabled' => true ) ) ) );
		same( array( 'blocks/0/items/0/acf_fc_layout_disabled' ), array_column( $on_repeater['errors'], 'path' ), 'a repeater row has no reserved keys' );
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
		same( array( 'blocks/1/items/3/label', 'blocks/0/body' ), $result['missing_required'], 'missing_required names the paths in the new value, after the later insert shifted the list row' );
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

		// A disabled row counts toward nothing.
		$off = apply_ok( blocks_field(), $texts['value'], array( array( 'op' => 'insert', 'path' => 'blocks/0', 'value' => array( 'acf_fc_layout' => 'text', 'body' => 'z', 'acf_fc_layout_disabled' => true ) ) ) );
		same( 3, count( array_keys( array_column( $off['value'], 'acf_fc_layout' ), 'text' ) ), 'a third text row, disabled, passes a layout max of 2' );
		$five        = array_fill( 0, 5, array( 'acf_fc_layout' => 'hero', 'image' => 1 ) );
		$full_blocks = istota_fields_apply( blocks_field(), array(), array( array( 'op' => 'set', 'path' => 'blocks', 'value' => $five ) ) );
		same( array(), $full_blocks['errors'], 'five rows fit a max of 5' );
		$six              = $five;
		$six[]            = array( 'acf_fc_layout' => 'hero', 'image' => 1 );
		$over_blocks      = istota_fields_apply( blocks_field(), array(), array( array( 'op' => 'set', 'path' => 'blocks', 'value' => $six ) ) );
		ok( 1 === count( $over_blocks['errors'] ), 'six enabled rows do not' );
		$six[5]['acf_fc_layout_disabled'] = true;
		$with_disabled    = istota_fields_apply( blocks_field(), array(), array( array( 'op' => 'set', 'path' => 'blocks', 'value' => $six ) ) );
		same( array(), $with_disabled['errors'], 'a sixth, disabled row counts toward nothing' );

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

test(
	'leaves are what a written value holds, never its containers',
	function () {
		$row    = array(
			'acf_fc_layout'          => 'list',
			'acf_fc_layout_disabled' => true,
			'title'                  => 'T',
			'items'                  => array( array( 'label' => 'a', 'done' => true ) ),
			'options'                => array( 'spacing' => 'lg' ),
			'stray'                  => 1,
		);
		$leaves = istota_fields_leaves( istota_fields_row_def( blocks_field(), 2, $row ), $row, 'blocks/2' );
		$paths  = array();
		foreach ( $leaves as $leaf ) {
			$paths[ $leaf['path'] ] = $leaf['field']['type'];
		}
		same(
			array(
				'blocks/2/title'             => 'text',
				'blocks/2/items/0/label'     => 'text',
				'blocks/2/items/0/done'      => 'true_false',
				'blocks/2/options/spacing'   => 'select',
			),
			$paths,
			'every present leaf, with its definition; reserved keys and unknown keys skipped'
		);
		same( 'a', $leaves[1]['value'], 'a leaf carries its written value' );
		same( array(), istota_fields_leaves( items_field(), 'not a list', 'items' ), 'a malformed list has no leaves' );
		$leaf = istota_fields_leaves( f( 'image', 'image' ), null, 'blocks/1/image' );
		same( array( array( 'path' => 'blocks/1/image', 'field' => f( 'image', 'image' ), 'value' => null ) ), $leaf, 'a leaf field written whole is its own leaf' );
	}
);

test(
	'each op reports where its node is in the new value',
	function () {
		$result = apply_ok(
			blocks_field(),
			blocks_value(),
			array(
				array( 'op' => 'set', 'path' => 'blocks/2/title', 'value' => 'Second edited' ),
				array( 'op' => 'insert', 'path' => 'blocks/0/items/-', 'value' => array( 'label' => 'd' ) ),
				array( 'op' => 'set', 'path' => 'blocks/0/items/1/label', 'value' => 'B' ),
				array( 'op' => 'insert', 'path' => 'blocks/0', 'value' => array( 'acf_fc_layout' => 'text', 'body' => 'x' ) ),
				array( 'op' => 'move', 'from' => 'blocks/1/items/3', 'path' => 'blocks/1/items/0' ),
				array( 'op' => 'remove', 'path' => 'blocks/2' ),
			)
		);
		same(
			array( 'blocks/2/title', 'blocks/1/items/0', 'blocks/1/items/2/label', 'blocks/0', 'blocks/1/items/0', null ),
			$result['paths'],
			'paths follow rows shifted by later inserts and moves; a remove has none'
		);
		$after = $result['value'];
		same( 'Second edited', $after[2]['title'], 'the first op\'s final path holds its value' );
		same( 'B', $after[1]['items'][2]['label'], 'the third op\'s final path holds its value' );
		$gone = apply_ok(
			blocks_field(),
			blocks_value(),
			array(
				array( 'op' => 'set', 'path' => 'blocks/2/title', 'value' => 'x' ),
				array( 'op' => 'remove', 'path' => 'blocks/2' ),
			)
		);
		same( array( null, null ), $gone['paths'], 'a node a later op removed has no path' );
	}
);

test(
	'row ops refuse a list holding a row of an undefined layout',
	function () {
		$raw   = raw_blocks();
		$raw[] = array( 'acf_fc_layout' => 'retired', 'field_old' => 'kept' );
		$value = istota_fields_normalize( blocks_field(), $raw );
		foreach ( array(
			array( 'op' => 'insert', 'path' => 'blocks/0', 'value' => array( 'acf_fc_layout' => 'text', 'body' => 'x' ) ),
			array( 'op' => 'remove', 'path' => 'blocks/1' ),
			array( 'op' => 'move', 'from' => 'blocks/0', 'path' => 'blocks/2' ),
		) as $op ) {
			$e = raised(
				function () use ( $value, $op ) {
					istota_fields_apply( blocks_field(), $value, array( $op ) );
				}
			);
			ok( $e && 'validation_error' === $e->reason, $op['op'] . ' refused beside an undefined layout' );
		}
		$set = apply_ok( blocks_field(), $value, array( array( 'op' => 'set', 'path' => 'blocks/0/title', 'value' => 'ok' ) ) );
		same( 'ok', $set['value'][0]['title'], 'a set, which shifts nothing, still applies' );
		$inner = apply_ok( blocks_field(), blocks_value(), array( array( 'op' => 'insert', 'path' => 'blocks/0/items/0', 'value' => array( 'label' => 'n' ) ) ) );
		same( 'n', $inner['value'][0]['items'][0]['label'], 'a list with only known layouts is untouched by the rule' );
	}
);

test(
	'a required leaf is refused only where the edit empties it (Decision 15)',
	function () {
		$raw                  = raw_blocks();
		$raw[1]['field_body'] = '';
		$value                = istota_fields_normalize( blocks_field(), $raw );
		same( null, $value[1]['body'], 'fixture: the required body is empty' );

		// Copy, change one sub-field, write the whole row back.
		$row          = $value[1];
		$row['image'] = 7;
		$result       = apply_ok( blocks_field(), $value, array( array( 'op' => 'set', 'path' => 'blocks/1', 'value' => $row ) ) );
		same( array(), $result['errors'], 'a whole-row set keeping an empty required leaf empty is not refused' );
		same( array( 'blocks/1/body' ), $result['missing_required'], 'it is reported in missing_required' );
		same( 7, $result['value'][1]['image'], 'the changed sub-field applies' );

		$direct = apply_ok( blocks_field(), $value, array( array( 'op' => 'set', 'path' => 'blocks/1/body', 'value' => '' ) ) );
		same( array(), $direct['errors'], 'setting an empty required leaf to empty is not refused' );
		same( array( 'blocks/1/body' ), $direct['missing_required'], 'and is reported' );

		unset( $row['body'] );
		$left = apply_ok( blocks_field(), $value, array( array( 'op' => 'set', 'path' => 'blocks/1', 'value' => $row ) ) );
		same( array(), $left['errors'], 'leaving out a required leaf that was empty is not refused' );
		same( array( 'blocks/1/body' ), $left['missing_required'], 'and is reported' );

		// Emptying a required leaf that held something is still refused, every way.
		$full = blocks_value();
		foreach ( array(
			array( 'op' => 'set', 'path' => 'blocks/1/body', 'value' => null ),
			array( 'op' => 'set', 'path' => 'blocks/1/body', 'value' => '' ),
			array( 'op' => 'set', 'path' => 'blocks/1', 'value' => array( 'acf_fc_layout' => 'text', 'body' => null ) ),
			array( 'op' => 'set', 'path' => 'blocks/1', 'value' => array( 'acf_fc_layout' => 'text' ) ),
			array( 'op' => 'set', 'path' => 'blocks/0/items/0/label', 'value' => null ),
		) as $op ) {
			$refused = istota_fields_apply( blocks_field(), $full, array( $op ) );
			ok( count( $refused['errors'] ) > 0, 'emptying a filled required leaf is refused: ' . json_encode( $op ) );
			same( array(), $refused['missing_required'], 'and not reported as kept: ' . $op['path'] );
		}

		// Shape is still checked on an unchanged empty required leaf.
		$bad = istota_fields_apply( blocks_field(), $value, array( array( 'op' => 'set', 'path' => 'blocks/1', 'value' => array( 'acf_fc_layout' => 'text', 'body' => null, 'image' => 'x' ) ) ) );
		ok( 1 === count( $bad['errors'] ) && 'blocks/1/image' === $bad['errors'][0]['path'], 'shape errors elsewhere in the row still refuse' );

		// An insert replaces nothing: a required leaf written empty is reported.
		$ins = apply_ok( blocks_field(), $full, array( array( 'op' => 'insert', 'path' => 'blocks/0', 'value' => array( 'acf_fc_layout' => 'text', 'body' => null ) ) ) );
		same( array( 'blocks/0/body' ), $ins['missing_required'], 'an inserted row\'s empty required leaf is reported' );

		// The strict check, without a before value, is unchanged.
		ok( count( istota_fields_check_shape( f( 'text', 'x', array( 'required' => 1 ) ), null, 'x' ) ) === 1, 'check_shape still refuses a required null' );
	}
);

// ---------------------------------------------------------------------------
// The storage plan (Decision 18).

/** The plan of an edit, by path in the new value. */
function plan_of( $field, $value, $ops ) {
	$result = apply_ok( $field, $value, $ops );
	$out    = array();
	foreach ( $result['storage'] as $node ) {
		ok( ! isset( $out[ $node['path'] ] ), 'one plan entry per path: ' . $node['path'] );
		$out[ $node['path'] ] = $node;
	}
	return $out;
}

/** [name, source, named] of one plan entry, to compare at a glance. */
function where_from( $plan, $path ) {
	if ( ! isset( $plan[ $path ] ) ) {
		return 'absent';
	}
	return array( $plan[ $path ]['name'], $plan[ $path ]['source'], $plan[ $path ]['named'] );
}

test(
	'an insert names what it gives and the list; shifted rows keep their source',
	function () {
		$plan = plan_of( people_field(), people( array( 'A', 'B' ) ), array( array( 'op' => 'insert', 'path' => 'people/0', 'value' => array( 'name' => 'Z' ) ) ) );
		same( array( 'people', 'people', true ), where_from( $plan, 'people' ), 'the list is named' );
		same( array( 'people_0_name', null, true ), where_from( $plan, 'people/0/name' ), 'a given field of the new row is named' );
		same( array( 'people_0_age', null, false ), where_from( $plan, 'people/0/age' ), 'a left-out field of the new row has no source and is not named' );
		same( array( 'people_1_name', 'people_0_name', false ), where_from( $plan, 'people/1/name' ), 'a shifted row is stored at its new index, from its old one' );
		same( array( 'people_2_age', 'people_1_age', false ), where_from( $plan, 'people/2/age' ), 'and every field of it' );
		same( 7, count( $plan ), 'one entry per stored node: the list and two fields for each of three rows' );
	}
);

test(
	'a set names its node, below it and above it, and nothing beside it',
	function () {
		$plan = plan_of( blocks_field(), blocks_value(), array( array( 'op' => 'set', 'path' => 'blocks/0/items/1/label', 'value' => 'B' ) ) );
		same( array( 'blocks_0_items_1_label', 'blocks_0_items_1_label', true ), where_from( $plan, 'blocks/0/items/1/label' ), 'the leaf' );
		same( true, $plan['blocks/0/items']['named'], 'the list holding it' );
		same( true, $plan['blocks']['named'], 'the top-level field' );
		same( array( 'blocks_0_items_1_done', 'blocks_0_items_1_done', false ), where_from( $plan, 'blocks/0/items/1/done' ), 'a sibling leaf is not named' );
		same( false, $plan['blocks/0/items/0/label']['named'], 'nor a sibling row' );
		same( false, $plan['blocks/0/title']['named'], 'nor the row\'s other fields' );
		same( 'absent', where_from( $plan, 'blocks/0' ), 'a row has no storage node of its own' );

		// A group's sub-fields are stored under the group's name.
		same( array( 'blocks_2_options_spacing', 'blocks_2_options_spacing', false ), where_from( $plan, 'blocks/2/options/spacing' ), 'group naming' );

		$whole = plan_of( blocks_field(), blocks_value(), array( array( 'op' => 'set', 'path' => 'blocks/2/options', 'value' => array( 'background' => '#000', 'spacing' => 'lg' ) ) ) );
		same( true, $whole['blocks/2/options/background']['named'] && $whole['blocks/2/options']['named'], 'a set of a group names all of it' );
		same( false, $whole['blocks/0/options/background']['named'], 'and not the same group in another row' );
	}
);

test(
	'row ops: sources follow rows through moves and removes',
	function () {
		$value = blocks_value();
		$plan  = plan_of( blocks_field(), $value, array( array( 'op' => 'move', 'from' => 'blocks/0', 'path' => 'blocks/2' ) ) );
		same( array( 'blocks_2_title', 'blocks_0_title', false ), where_from( $plan, 'blocks/2/title' ), 'the moved row is stored at its new index, from its old one' );
		same( array( 'blocks_0_body', 'blocks_1_body', false ), where_from( $plan, 'blocks/0/body' ), 'the rows it passed shift back' );
		same( array( 'blocks_2_items_2_label', 'blocks_0_items_2_label', false ), where_from( $plan, 'blocks/2/items/2/label' ), 'nested rows go with it' );
		same( true, $plan['blocks']['named'], 'the list a move acts in is named' );

		$plan = plan_of( blocks_field(), $value, array( array( 'op' => 'remove', 'path' => 'blocks/0' ) ) );
		same( array( 'blocks_1_title', 'blocks_2_title', false ), where_from( $plan, 'blocks/1/title' ), 'a remove shifts the rows after it' );
		same( 'absent', where_from( $plan, 'blocks/2/title' ), 'and the last index is gone' );

		$plan = plan_of( blocks_field(), $value, array( array( 'op' => 'remove', 'path' => 'blocks/0/items/1' ) ) );
		same( true, $plan['blocks/0/items']['named'] && $plan['blocks']['named'], 'a remove names its list and what holds it' );
		same( false, $plan['blocks/2/items']['named'], 'and not another row\'s list' );
	}
);

test(
	'an inserted row with a nested list names only what it gives, at any depth',
	function () {
		$row  = array(
			'acf_fc_layout' => 'list',
			'items'         => array( array( 'label' => 'n' ) ),
		);
		$plan = plan_of( blocks_field(), blocks_value(), array( array( 'op' => 'insert', 'path' => 'blocks/1', 'value' => $row ) ) );
		same( array( 'blocks_1_items', null, true ), where_from( $plan, 'blocks/1/items' ), 'a given list is named' );
		same( array( 'blocks_1_items_0_label', null, true ), where_from( $plan, 'blocks/1/items/0/label' ), 'a given field of its row is named' );
		same( array( 'blocks_1_items_0_done', null, false ), where_from( $plan, 'blocks/1/items/0/done' ), 'a field its row leaves out is not' );
		same( array( 'blocks_1_title', null, false ), where_from( $plan, 'blocks/1/title' ), 'nor one the inserted row leaves out' );
		same( array( 'blocks_1_options_background', null, false ), where_from( $plan, 'blocks/1/options/background' ), 'nor a left-out group\'s fields' );
		same( array( 'blocks_2_body', 'blocks_1_body', false ), where_from( $plan, 'blocks/2/body' ), 'the row it pushed down keeps its source' );

		// Ops later in the edit shift the inserted row; what it gave stays named.
		$plan = plan_of(
			blocks_field(),
			blocks_value(),
			array(
				array( 'op' => 'insert', 'path' => 'blocks/1', 'value' => $row ),
				array( 'op' => 'insert', 'path' => 'blocks/0', 'value' => array( 'acf_fc_layout' => 'text' ) ),
			)
		);
		same( array( 'blocks_2_items_0_label', null, true ), where_from( $plan, 'blocks/2/items/0/label' ), 'a given field stays named after a later shift' );
		same( array( 'blocks_2_items_0_done', null, false ), where_from( $plan, 'blocks/2/items/0/done' ), 'and a left-out one unnamed' );
	}
);

test(
	'rows a set adds stay named when a later op shifts them',
	function () {
		$items = array( array( 'label' => 'a' ), array( 'label' => 'b' ), array( 'label' => 'c' ), array( 'label' => 'd' ) );
		$plan  = plan_of(
			blocks_field(),
			blocks_value(),
			array(
				array( 'op' => 'set', 'path' => 'blocks/2/items', 'value' => $items ),
				array( 'op' => 'insert', 'path' => 'blocks/0', 'value' => array( 'acf_fc_layout' => 'text' ) ),
			)
		);
		same( array( 'blocks_3_items_3_label', null, true ), where_from( $plan, 'blocks/3/items/3/label' ), 'a row a set added is named where it ends up' );
		same( array( 'blocks_3_items_0_label', 'blocks_2_items_0_label', true ), where_from( $plan, 'blocks/3/items/0/label' ), 'a row a set replaced keeps its source' );
		same( false, $plan['blocks/3/title']['named'], 'the rest of that row is not named' );
	}
);

test(
	'clone fields are stored under the clone\'s own prefix',
	function () {
		$typography = array(
			'key'        => 'field_typo',
			'name'       => 'typography',
			'_name'      => 'typography',
			'label'      => 'Typography',
			'type'       => 'clone',
			'display'    => 'group',
			'required'   => 0,
			'sub_fields' => array( f( 'range', 'body_font_size' ) ),
		);
		$prefixed                           = $typography;
		$prefixed['name']                   = 'heading';
		$prefixed['_name']                  = 'heading';
		$prefixed['key']                    = 'field_head';
		$prefixed['sub_fields'][0]['name']  = 'heading_size';
		$prefixed['sub_fields'][0]['key']   = 'field_head_size';
		$rows                               = f( 'repeater', 'rows', array( 'sub_fields' => array( f( 'text', 'x' ), $typography, $prefixed ) ) );
		$value                              = array(
			array(
				'x'          => 'a',
				'typography' => array( 'body_font_size' => 1 ),
				'heading'    => array( 'heading_size' => 2 ),
			),
		);
		$plan = plan_of( $rows, $value, array( array( 'op' => 'set', 'path' => 'rows/0/x', 'value' => 'b' ) ) );
		same( array( 'rows_0_typography', 'rows_0_typography', false ), where_from( $plan, 'rows/0/typography' ), 'the clone has a row of its own' );
		same( array( 'rows_0_body_font_size', 'rows_0_body_font_size', false ), where_from( $plan, 'rows/0/typography/body_font_size' ), 'a cloned field is stored without the clone\'s name' );
		same( 'rows_0_heading_size', $plan['rows/0/heading/heading_size']['name'], 'prefix_name is in the name it was given' );

		$top  = plan_of( $typography, array( 'body_font_size' => 1 ), array( array( 'op' => 'set', 'path' => 'typography/body_font_size', 'value' => 2 ) ) );
		same( 'body_font_size', $top['typography/body_font_size']['name'], 'a top-level clone\'s fields carry no prefix' );

		// A clone inside a prefix_name clone: SCF names the inner one outer_inner and
		// cuts its own _name off to get its fields' prefix, so they are not the row's.
		$inner = array(
			'key'        => 'field_inner',
			'name'       => 'outer_inner',
			'_name'      => 'inner',
			'label'      => 'Inner',
			'type'       => 'clone',
			'display'    => 'group',
			'required'   => 0,
			'sub_fields' => array( f( 'text', 'title' ) ),
		);
		$outer = array(
			'key'        => 'field_outer',
			'name'       => 'outer',
			'_name'      => 'outer',
			'label'      => 'Outer',
			'type'       => 'clone',
			'display'    => 'group',
			'required'   => 0,
			'sub_fields' => array( $inner ),
		);
		$list  = f( 'repeater', 'rows', array( 'sub_fields' => array( f( 'text', 'title' ), $outer ) ) );
		$plan  = plan_of( $list, array( array( 'title' => 'a', 'outer' => array( 'outer_inner' => array( 'title' => 'b' ) ) ) ), array( array( 'op' => 'insert', 'path' => 'rows/0', 'value' => array( 'title' => 'Hello' ) ) ) );
		same( 'rows_0_title', $plan['rows/0/title']['name'], 'the row\'s own field' );
		same( 'rows_0_outer_title', $plan['rows/0/outer/outer_inner/title']['name'], 'the nested clone\'s field, under the outer prefix' );
		$names = array();
		foreach ( $plan as $node ) {
			$names[] = $node['name'];
		}
		same( count( $names ), count( array_unique( $names ) ), 'no two nodes share a name' );
	}
);

test(
	'a flexible field is clean only with no row disabled or renamed',
	function () {
		$value = blocks_value();
		$plan  = plan_of( blocks_field(), $value, array( array( 'op' => 'set', 'path' => 'blocks/0/title', 'value' => 'x' ) ) );
		same( true, $plan['blocks']['flexible'] && $plan['blocks']['clean'], 'no flags: clean' );
		same( false, $plan['blocks/0/items']['flexible'], 'a repeater is not flexible' );

		$value[1]['acf_fc_layout_disabled'] = true;
		$plan                               = plan_of( blocks_field(), $value, array( array( 'op' => 'set', 'path' => 'blocks/0/title', 'value' => 'x' ) ) );
		same( false, $plan['blocks']['clean'], 'a disabled row: not clean' );

		$value[1]['acf_fc_layout_disabled']     = false;
		$value[1]['acf_fc_layout_custom_label'] = 'Mine';
		$plan                                   = plan_of( blocks_field(), $value, array( array( 'op' => 'set', 'path' => 'blocks/0/title', 'value' => 'x' ) ) );
		same( false, $plan['blocks']['clean'], 'a renamed row: not clean' );
	}
);

test(
	'a whole-field set names what it changes and what it adds',
	function () {
		$plan = plan_of( people_field(), people( array( 'A' ) ), array( array( 'op' => 'set', 'path' => 'people', 'value' => people( array( 'B', 'C' ) ) ) ) );
		same( true, $plan['people']['named'], 'the field itself' );
		same( true, $plan['people/0/name']['named'], 'a leaf it changed' );
		same( false, $plan['people/0/age']['named'], 'a leaf it wrote back as read is not named' );
		same( true, $plan['people/1/name']['named'] && $plan['people/1/age']['named'], 'a row it added is named whole' );
		same( null, $plan['people/1/name']['source'], 'a row the set added has no source' );
	}
);

test(
	'a whole-row set copied from a read names only the sub-field it changed',
	function () {
		// What `fields get` hands back for row 0, with one sub-field changed.
		$value         = blocks_value();
		$row           = $value[0];
		$row['title']  = 'Changed';
		$plan          = plan_of( blocks_field(), $value, array( array( 'op' => 'set', 'path' => 'blocks/0', 'value' => $row ) ) );
		same( true, $plan['blocks/0/title']['named'], 'the changed sub-field is named' );
		same( true, $plan['blocks']['named'], 'and the list holding the row' );
		same( false, $plan['blocks/0/items/0/link']['named'], 'an unchanged sub-field with nothing stored is not' );
		same( false, $plan['blocks/0/items']['named'], 'nor an unchanged nested list' );
		same( false, $plan['blocks/0/options/background']['named'], 'nor an unchanged group field' );

		// A sub-field read as nothing stored (null) that the set fills is named,
		// so its new row stays.
		$row                     = $value[0];
		$row['items'][0]['link'] = array(
			'url'    => 'https://example.test/',
			'title'  => 'x',
			'target' => '',
		);
		$plan                    = plan_of( blocks_field(), $value, array( array( 'op' => 'set', 'path' => 'blocks/0', 'value' => $row ) ) );
		same( null, $value[0]['items'][0]['link'], 'the fixture read the link as nothing stored' );
		same( true, $plan['blocks/0/items/0/link']['named'], 'a sub-field the set fills from nothing stored is named' );
		same( true, $plan['blocks/0/items']['named'], 'and so is the list it changed' );
		same( false, $plan['blocks/0/items/1/link']['named'], 'its unchanged sibling is not' );

		// The node a set addresses is named even when written as it was.
		$plan = plan_of( blocks_field(), $value, array( array( 'op' => 'set', 'path' => 'blocks/0/title', 'value' => 'First' ) ) );
		same( true, $plan['blocks/0/title']['named'], 'a set leaf is named, changed or not' );
	}
);

$total = $GLOBALS['passes'] + $GLOBALS['failures'];
if ( $GLOBALS['failures'] ) {
	fwrite( STDERR, $GLOBALS['failures'] . " of $total checks failed\n" );
	exit( 1 );
}
echo "$total checks passed\n";
exit( 0 );
