<?php
/**
 * Plugin Name: Istota Connector
 * Description: Registers Abilities API abilities the Istota wordpress skill uses where core REST has no route: read and write an ACF options page, read and edit ACF values on a post or options page by path, and list a multisite network's sites.
 * Version: 0.2.0
 * Requires at least: 6.9
 * Requires PHP: 7.4
 * Author: Istota
 * License: EUPL-1.2
 * Network: true
 * Text Domain: istota-connector
 *
 * Every ability is registered with an input and output schema, a permission
 * callback and annotations, and shown in REST, so the skill reaches it through
 * /wp-abilities/v1/abilities/{name}/run like any other ability.
 *
 * - istota/options-get: the fields of one ACF options page. manage_options,
 *   plus the page's own capability.
 * - istota/options-update: write those fields, each value replaced whole.
 *   Same permission. Not destructive: it overwrites values, it removes nothing.
 * - istota/network-sites: the network's sites. manage_sites on the main site.
 * - istota/fields-get: the ACF fields of a post or options page, or the
 *   value and definition of one node in one of them, with a token. edit_post
 *   on the post, or the options page permission above.
 * - istota/fields-edit: ordered set, insert, remove and move operations on one
 *   top-level field, refused when the token is stale. Same permission. Only
 *   what the operations write is validated, and nothing they do not name is
 *   touched. includes/fields.php holds the value model.
 *
 * Only fields in a field group whose "Show in REST API" setting is on are
 * reachable, the same rule ACF applies to posts over REST. A field in any
 * other group can be neither read nor written through these abilities.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

const ISTOTA_CONNECTOR_MAX_SITES = 1000;

require_once __DIR__ . '/includes/fields.php';

add_action( 'wp_abilities_api_categories_init', 'istota_connector_register_category' );
add_action( 'wp_abilities_api_init', 'istota_connector_register_abilities' );

function istota_connector_register_category() {
	wp_register_ability_category(
		'istota',
		array(
			'label'       => 'Istota',
			'description' => 'Abilities the Istota assistant uses where core REST has no route.',
		)
	);
}

/**
 * The options page named by $slug, or a WP_Error.
 */
function istota_connector_options_page( $slug ) {
	if ( ! function_exists( 'acf_get_options_page' ) || ! function_exists( 'acf_get_field_groups' ) ) {
		return new WP_Error( 'istota_acf_missing', 'ACF or Secure Custom Fields with options pages is not active on this site.', array( 'status' => 400 ) );
	}
	$page = acf_get_options_page( $slug );
	if ( empty( $page ) || ! is_array( $page ) ) {
		return new WP_Error( 'istota_options_page_not_found', 'There is no options page with that slug.', array( 'status' => 404 ) );
	}
	return $page;
}

/**
 * The page's top-level fields, by name, from REST-visible field groups only.
 */
function istota_connector_rest_fields( $slug ) {
	$fields = array();
	foreach ( acf_get_field_groups( array( 'options_page' => $slug ) ) as $group ) {
		if ( empty( $group['show_in_rest'] ) ) {
			continue;
		}
		foreach ( acf_get_fields( $group ) as $field ) {
			if ( ! empty( $field['name'] ) ) {
				$fields[ $field['name'] ] = $field;
			}
		}
	}
	return $fields;
}

/**
 * A field's value in the shape ACF's REST API gives it on a post.
 */
function istota_connector_field_value( $field, $post_id ) {
	if ( function_exists( 'acf_format_value_for_rest' ) ) {
		$format = function_exists( 'acf_get_setting' ) ? acf_get_setting( 'rest_api_format' ) : 'light';
		return acf_format_value_for_rest( acf_get_value( $post_id, $field ), $post_id, $field, $format );
	}
	return get_field( $field['key'], $post_id, false );
}

function istota_connector_page_values( $slug, $post_id ) {
	$values = array();
	foreach ( istota_connector_rest_fields( $slug ) as $name => $field ) {
		$values[ $name ] = istota_connector_field_value( $field, $post_id );
	}
	return $values;
}

/**
 * manage_options on this site, and the options page's own capability.
 */
function istota_connector_can_edit_options( $input = null ) {
	if ( ! current_user_can( 'manage_options' ) ) {
		return false;
	}
	if ( is_array( $input ) && isset( $input['page'] ) && is_string( $input['page'] ) ) {
		$page = istota_connector_options_page( $input['page'] );
		if ( is_array( $page ) && ! empty( $page['capability'] ) && ! current_user_can( $page['capability'] ) ) {
			return false;
		}
	}
	return true;
}

function istota_connector_can_list_sites() {
	if ( ! is_multisite() ) {
		return new WP_Error( 'istota_not_multisite', 'This site is not a multisite network.', array( 'status' => 400 ) );
	}
	return current_user_can_for_site( get_main_site_id(), 'manage_sites' );
}

function istota_connector_options_get( $input ) {
	$page = istota_connector_options_page( $input['page'] );
	if ( is_wp_error( $page ) ) {
		return $page;
	}
	return array(
		'page'    => $input['page'],
		'post_id' => (string) $page['post_id'],
		'fields'  => (object) istota_connector_page_values( $input['page'], $page['post_id'] ),
	);
}

function istota_connector_options_update( $input ) {
	$page = istota_connector_options_page( $input['page'] );
	if ( is_wp_error( $page ) ) {
		return $page;
	}
	$fields = istota_connector_rest_fields( $input['page'] );

	// Every name and every value is checked before anything is written.
	$invalid = array();
	foreach ( $input['fields'] as $name => $value ) {
		if ( ! isset( $fields[ $name ] ) ) {
			$invalid[ $name ] = 'Not a REST-visible field of this options page.';
			continue;
		}
		$type = function_exists( 'acf_get_field_type' ) ? acf_get_field_type( $fields[ $name ]['type'] ) : null;
		if ( $type && method_exists( $type, 'get_rest_schema' ) ) {
			$checked = rest_validate_value_from_schema( $value, $type->get_rest_schema( $fields[ $name ] ), $name );
			if ( is_wp_error( $checked ) ) {
				$invalid[ $name ] = $checked->get_error_message();
			}
		}
	}
	if ( $invalid ) {
		return new WP_Error(
			'rest_invalid_param',
			'Invalid field(s): ' . implode( ', ', array_keys( $invalid ) ),
			array(
				'status' => 400,
				'params' => $invalid,
			)
		);
	}

	foreach ( $input['fields'] as $name => $value ) {
		update_field( $fields[ $name ]['key'], $value, $page['post_id'] );
	}
	return array(
		'page'    => $input['page'],
		'post_id' => (string) $page['post_id'],
		'fields'  => (object) istota_connector_page_values( $input['page'], $page['post_id'] ),
	);
}

function istota_connector_network_sites( $input ) {
	$number = ISTOTA_CONNECTOR_MAX_SITES;
	if ( is_array( $input ) && isset( $input['number'] ) ) {
		$number = max( 1, min( ISTOTA_CONNECTOR_MAX_SITES, absint( $input['number'] ) ) );
	}
	$sites = array();
	foreach ( get_sites( array( 'number' => $number, 'network_id' => get_current_network_id() ) ) as $site ) {
		$sites[] = array(
			'id'       => (int) $site->blog_id,
			'domain'   => $site->domain,
			'path'     => $site->path,
			'name'     => (string) $site->blogname,
			'public'   => (bool) $site->public,
			'archived' => (bool) $site->archived,
			'deleted'  => (bool) $site->deleted,
		);
	}
	return array(
		'total' => (int) get_sites( array( 'count' => true, 'network_id' => get_current_network_id() ) ),
		'sites' => $sites,
	);
}

// ---------------------------------------------------------------------------
// istota/fields-get and istota/fields-edit.

/** How each reason the value model throws is answered. */
function istota_connector_fields_error( Istota_Fields_Error $e ) {
	$answers = array(
		'validation_error'  => array( 'rest_invalid_param', 400 ),
		'unknown_path'      => array( 'istota_unknown_path', 404 ),
		'mixed_fields'      => array( 'istota_mixed_fields', 400 ),
		'unsupported_field' => array( 'istota_unsupported_field', 400 ),
	);
	$answer = isset( $answers[ $e->reason ] ) ? $answers[ $e->reason ] : array( 'rest_invalid_param', 400 );
	return new WP_Error(
		$answer[0],
		$e->getMessage(),
		array(
			'status' => $answer[1],
			'params' => (object) $e->params,
		)
	);
}

/** Whether $input names exactly one of post_id and page. */
function istota_connector_fields_input_ok( $input ) {
	return is_array( $input ) && ( isset( $input['post_id'] ) xor isset( $input['page'] ) );
}

/**
 * edit_post on the post, or the options page permission. A post that does not
 * exist or that the user cannot read is let through to the callback, which
 * answers it as not found, so that a 403 does not tell its existence apart.
 */
function istota_connector_can_edit_fields( $input = null ) {
	if ( ! istota_connector_fields_input_ok( $input ) ) {
		// Malformed: the callback answers it, to anyone who could edit something.
		return current_user_can( 'edit_posts' ) || current_user_can( 'manage_options' );
	}
	if ( isset( $input['page'] ) ) {
		return istota_connector_can_edit_options( $input );
	}
	$post_id = absint( $input['post_id'] );
	if ( ! get_post( $post_id ) || ! current_user_can( 'read_post', $post_id ) ) {
		return current_user_can( 'edit_posts' );
	}
	return current_user_can( 'edit_post', $post_id );
}

/**
 * What an edit or a read addresses: the ACF storage id, the post or the
 * options page, and the reachable top-level fields by name. Or a WP_Error.
 */
function istota_connector_fields_target( $input ) {
	if ( ! function_exists( 'acf_get_field_groups' ) || ! function_exists( 'acf_get_value' ) || ! function_exists( 'update_field' ) ) {
		return new WP_Error( 'istota_acf_missing', 'ACF or Secure Custom Fields is not active on this site.', array( 'status' => 400 ) );
	}
	if ( ! istota_connector_fields_input_ok( $input ) ) {
		return new WP_Error( 'rest_invalid_param', 'Give exactly one of post_id and page.', array( 'status' => 400 ) );
	}
	if ( isset( $input['page'] ) ) {
		$page = istota_connector_options_page( $input['page'] );
		if ( is_wp_error( $page ) ) {
			return $page;
		}
		return array(
			'storage' => $page['post_id'],
			'post'    => null,
			'page'    => $input['page'],
			'fields'  => istota_connector_rest_fields( $input['page'] ),
		);
	}
	$post_id = absint( $input['post_id'] );
	$post    = get_post( $post_id );
	if ( ! $post || ! current_user_can( 'read_post', $post_id ) ) {
		return new WP_Error( 'istota_post_not_found', 'There is no post with that id.', array( 'status' => 404 ) );
	}
	if ( ! current_user_can( 'edit_post', $post_id ) ) {
		return new WP_Error( 'rest_forbidden', 'Sorry, you are not allowed to edit this post.', array( 'status' => rest_authorization_required_code() ) );
	}
	$fields = array();
	foreach ( acf_get_field_groups( array( 'post_id' => $post_id ) ) as $group ) {
		if ( empty( $group['show_in_rest'] ) ) {
			continue;
		}
		foreach ( acf_get_fields( $group ) as $field ) {
			if ( ! empty( $field['name'] ) && istota_fields_has_value( $field ) ) {
				$fields[ $field['name'] ] = $field;
			}
		}
	}
	return array(
		'storage' => $post_id,
		'post'    => $post,
		'page'    => null,
		'fields'  => $fields,
	);
}

function istota_connector_not_in_rest( $name ) {
	return new WP_Error(
		'acf_not_in_rest',
		sprintf( 'There is no field %s in a field group shown in REST for this target.', $name ),
		array(
			'status' => 400,
			'params' => array( 'field' => $name ),
		)
	);
}

/**
 * A flexible content field's raw value with every row, disabled ones included,
 * for acf/pre_load_value while istota_connector_raw_value() reads.
 *
 * SCF's own load_value drops a disabled row on any read outside wp-admin, so a
 * value read through it and written back would delete the row. This mirrors
 * that load_value without the drop, and puts each row's disabled flag and
 * custom label on the row under the keys update_value reads them from. The
 * layout meta name follows the field's full storage name, which ACF has
 * already set for a nested field, so each nesting level reads its own.
 */
function istota_connector_flexible_rows( $pre, $post_id, $field ) {
	if ( null !== $pre || ! is_array( $field ) || ! isset( $field['type'] ) || 'flexible_content' !== $field['type'] || empty( $field['name'] ) ) {
		return $pre;
	}
	$value = acf_get_metadata_by_field( $post_id, $field );
	if ( empty( $value ) || empty( $field['layouts'] ) ) {
		return array();
	}
	$meta     = acf_get_metadata_by_field( $post_id, array( 'name' => '_' . $field['name'] . '_layout_meta' ) );
	$disabled = array();
	$renamed  = array();
	if ( is_array( $meta ) ) {
		foreach ( ( isset( $meta['disabled'] ) && is_array( $meta['disabled'] ) ) ? $meta['disabled'] : array() as $index ) {
			$disabled[ (int) $index ] = true;
		}
		$renamed = ( isset( $meta['renamed'] ) && is_array( $meta['renamed'] ) ) ? $meta['renamed'] : array();
	}
	$layouts = array();
	foreach ( $field['layouts'] as $layout ) {
		$layouts[ $layout['name'] ] = isset( $layout['sub_fields'] ) && is_array( $layout['sub_fields'] ) ? $layout['sub_fields'] : array();
	}
	$rows = array();
	foreach ( acf_get_array( $value ) as $i => $name ) {
		$row = array(
			'acf_fc_layout'              => $name,
			'acf_fc_layout_disabled'     => isset( $disabled[ (int) $i ] ),
			'acf_fc_layout_custom_label' => isset( $renamed[ $i ] ) ? $renamed[ $i ] : null,
		);
		foreach ( isset( $layouts[ $name ] ) ? $layouts[ $name ] : array() as $sub_field ) {
			if ( acf_is_empty( $sub_field['name'] ) ) {
				continue;
			}
			$sub_field['name']        = "{$field['name']}_{$i}_{$sub_field['name']}";
			$row[ $sub_field['key'] ] = acf_get_value( $post_id, $sub_field );
		}
		$rows[ $i ] = $row;
	}
	return $rows;
}

/**
 * A top-level field's raw stored value, as acf_get_value() loads it but with
 * every flexible content row at every level. ACF's value store is emptied on
 * both sides, so neither a value cached by an earlier read nor this read's
 * disabled rows reach any other reader.
 */
function istota_connector_raw_value( $storage, array $field ) {
	acf_get_store( 'values' )->reset();
	add_filter( 'acf/pre_load_value', 'istota_connector_flexible_rows', 10, 3 );
	try {
		$raw = acf_get_value( $storage, $field );
	} finally {
		remove_filter( 'acf/pre_load_value', 'istota_connector_flexible_rows', 10 );
		acf_get_store( 'values' )->reset();
	}
	return $raw;
}

/** A top-level field's normalized value, or a WP_Error. */
function istota_connector_normalized( $storage, array $field ) {
	try {
		return istota_fields_normalize( $field, istota_connector_raw_value( $storage, $field ) );
	} catch ( Istota_Fields_Error $e ) {
		return istota_connector_fields_error( $e );
	}
}

function istota_connector_token_of( $value ) {
	try {
		return istota_fields_token( $value );
	} catch ( Istota_Fields_Error $e ) {
		return istota_connector_fields_error( $e );
	}
}

/** What fields-get and fields-edit say about the target in every answer. */
function istota_connector_fields_context( array $target ) {
	$out = array(
		'post_id'      => (string) $target['storage'],
		'post_status'  => null,
		'post_type'    => null,
		'modified_gmt' => null,
		'locked_by'    => null,
	);
	if ( $target['post'] ) {
		clean_post_cache( $target['post']->ID );
		$post                = get_post( $target['post']->ID );
		$out['post_status']  = $post->post_status;
		$out['post_type']    = $post->post_type;
		$out['modified_gmt'] = $post->post_modified_gmt;
		if ( ! function_exists( 'wp_check_post_lock' ) ) {
			require_once ABSPATH . 'wp-admin/includes/post.php';
		}
		$holder = wp_check_post_lock( $post->ID );
		if ( $holder ) {
			$user             = get_userdata( $holder );
			$out['locked_by'] = $user ? $user->display_name : (string) $holder;
		}
	}
	return $out;
}

function istota_connector_fields_get( $input ) {
	$target = istota_connector_fields_target( $input );
	if ( is_wp_error( $target ) ) {
		return $target;
	}
	$out = istota_connector_fields_context( $target );

	if ( ! isset( $input['path'] ) ) {
		$out['fields'] = array();
		foreach ( $target['fields'] as $name => $field ) {
			$value = istota_connector_normalized( $target['storage'], $field );
			$token = is_wp_error( $value ) ? null : istota_connector_token_of( $value );
			$out['fields'][] = array(
				'name'  => (string) $name,
				'type'  => (string) $field['type'],
				'label' => isset( $field['label'] ) ? (string) $field['label'] : '',
				// A field the value model refuses is listed, without a token.
				'token' => is_string( $token ) ? $token : null,
			);
		}
		return $out;
	}

	try {
		$segments = istota_fields_parse_path( $input['path'] );
	} catch ( Istota_Fields_Error $e ) {
		return istota_connector_fields_error( $e );
	}
	if ( ! isset( $target['fields'][ $segments[0] ] ) ) {
		return istota_connector_not_in_rest( $segments[0] );
	}
	$field = $target['fields'][ $segments[0] ];
	$value = istota_connector_normalized( $target['storage'], $field );
	if ( is_wp_error( $value ) ) {
		return $value;
	}
	$token = istota_connector_token_of( $value );
	if ( is_wp_error( $token ) ) {
		return $token;
	}
	$node = istota_fields_resolve( $field, $value, $segments );
	if ( ! $node['found'] ) {
		return istota_connector_fields_error(
			new Istota_Fields_Error(
				'unknown_path',
				sprintf( '%s does not exist; %s is the deepest part that does.', $input['path'], $node['exists'] ),
				array(
					'path'   => $input['path'],
					'exists' => $node['exists'],
				)
			)
		);
	}
	$out['path']       = $input['path'];
	$out['value']      = $node['value'];
	$out['token']      = $token;
	$out['definition'] = istota_fields_definition( $node['field'] );
	return $out;
}

/** $value with every string run through kses, for a user the editor would filter. */
function istota_connector_kses_strings( $value ) {
	if ( is_string( $value ) ) {
		return wp_kses_post( $value );
	}
	if ( is_array( $value ) ) {
		foreach ( $value as $key => $item ) {
			$value[ $key ] = istota_connector_kses_strings( $item );
		}
	}
	return $value;
}

/**
 * Whether a referenced id names what its field allows: an attachment for an
 * image, file or gallery, a post of an allowed type, a user, a term of the
 * field's taxonomy. A page_link URL string is not an id and is let through.
 */
function istota_connector_reference_ok( array $field, $id ) {
	if ( ! is_int( $id ) ) {
		return true;
	}
	switch ( $field['type'] ) {
		case 'image':
		case 'file':
		case 'gallery':
			$post = get_post( $id );
			return $post && 'attachment' === $post->post_type;
		case 'post_object':
		case 'page_link':
		case 'relationship':
			$post = get_post( $id );
			if ( ! $post ) {
				return false;
			}
			$types = empty( $field['post_type'] ) ? array() : (array) $field['post_type'];
			return ! $types || in_array( $post->post_type, $types, true );
		case 'user':
			return (bool) get_userdata( $id );
		case 'taxonomy':
			$term = get_term( $id, isset( $field['taxonomy'] ) ? $field['taxonomy'] : '' );
			return $term && ! is_wp_error( $term );
	}
	return true;
}

/**
 * Whether every value a choice field holds is one of its choices. ACF checks
 * no choices on save, since its form only offers them; a value from here could
 * be anything. A field that accepts custom values, or whose choices are filled
 * in somewhere this read cannot see (none loaded), is let through.
 */
function istota_connector_choices_ok( array $field, $value ) {
	if ( ! in_array( $field['type'], array( 'select', 'radio', 'button_group', 'checkbox' ), true ) ) {
		return true;
	}
	if ( ! empty( $field['allow_custom'] ) || ! empty( $field['other_choice'] ) || empty( $field['choices'] ) || ! is_array( $field['choices'] ) ) {
		return true;
	}
	$allowed = array_map( 'strval', array_keys( $field['choices'] ) );
	foreach ( is_array( $value ) ? $value : array( $value ) as $item ) {
		if ( null !== $item && ! in_array( (string) $item, $allowed, true ) ) {
			return false;
		}
	}
	return true;
}

/**
 * The WordPress half of validation, for every leaf an op wrote: ACF's own
 * acf_validate_value() (type rules and the site's acf/validate_value filters),
 * the choices, and the reference checks. Errors as the value model reports them.
 */
function istota_connector_check_written( array $written, array $skip_ops ) {
	$errors = array();
	acf_reset_validation_errors();
	foreach ( $written as $entry ) {
		$label = 'ops[' . $entry['op'] . ']';
		if ( isset( $skip_ops[ $label ] ) ) {
			// Its shape is already refused; ACF's checks would only restate it.
			continue;
		}
		foreach ( istota_fields_leaves( $entry['field'], $entry['value'], $entry['path'] ) as $leaf ) {
			$field = $leaf['field'];
			$valid = acf_validate_value( istota_fields_denormalize( $field, $leaf['value'] ), $field, $leaf['path'] );
			if ( ! $valid ) {
				$message = sprintf( '%s is not valid.', $leaf['path'] );
				foreach ( (array) acf_get_validation_errors() as $error ) {
					if ( isset( $error['input'] ) && $error['input'] === $leaf['path'] && ! empty( $error['message'] ) ) {
						$message = wp_strip_all_tags( (string) $error['message'] );
					}
				}
				$errors[] = array(
					'op'      => $label,
					'path'    => $leaf['path'],
					'message' => $message,
				);
				continue;
			}
			if ( ! istota_connector_choices_ok( $field, $leaf['value'] ) ) {
				$errors[] = array(
					'op'      => $label,
					'path'    => $leaf['path'],
					'message' => 'Not one of the field\'s choices; fields-get with this path lists them.',
				);
				continue;
			}
			$ids  = is_array( $leaf['value'] ) ? $leaf['value'] : array( $leaf['value'] );
			$kind = istota_fields_kind( $field );
			if ( ! in_array( $kind, array( 'id', 'ids', 'ref', 'refs' ), true ) ) {
				continue;
			}
			foreach ( $ids as $id ) {
				if ( null !== $id && ! istota_connector_reference_ok( $field, $id ) ) {
					$errors[] = array(
						'op'      => $label,
						'path'    => $leaf['path'],
						'message' => sprintf( '%d is not something this field can point at.', $id ),
					);
				}
			}
		}
	}
	acf_reset_validation_errors();
	return $errors;
}

/**
 * A post's modified time and its caches after a field write, then
 * acf/save_post for the site's own hooks, as an editor save would fire it.
 * Not wp_update_post(): it would run post_content through the save filters.
 */
function istota_connector_after_write( array $target ) {
	global $wpdb;
	if ( $target['post'] ) {
		$wpdb->update(
			$wpdb->posts,
			array(
				'post_modified'     => current_time( 'mysql' ),
				'post_modified_gmt' => current_time( 'mysql', true ),
			),
			array( 'ID' => $target['post']->ID )
		);
		clean_post_cache( $target['post']->ID );
	}
	do_action( 'acf/save_post', $target['storage'] );
}

function istota_connector_fields_edit( $input ) {
	$target = istota_connector_fields_target( $input );
	if ( is_wp_error( $target ) ) {
		return $target;
	}
	$ops = array_values( $input['ops'] );

	// 1. The field, from the first op; every op on that same field.
	$names = array();
	foreach ( $ops as $i => $op ) {
		$path    = ( is_array( $op ) && isset( $op['path'] ) && is_string( $op['path'] ) ) ? $op['path'] : '';
		$names[] = array( 'ops[' . $i . ']', explode( '/', $path, 2 )[0] );
		if ( is_array( $op ) && isset( $op['from'] ) && is_string( $op['from'] ) ) {
			$names[] = array( 'ops[' . $i . ']', explode( '/', $op['from'], 2 )[0] );
		}
	}
	$name = $names[0][1];
	foreach ( $names as $entry ) {
		if ( $entry[1] !== $name ) {
			return istota_connector_fields_error(
				new Istota_Fields_Error(
					'mixed_fields',
					sprintf( 'Every operation of one edit addresses the same top-level field, %s; %s addresses %s. Run one edit per field.', $name, $entry[0], $entry[1] ),
					array(
						'op'    => $entry[0],
						'field' => $entry[1],
					)
				)
			);
		}
	}
	if ( ! isset( $target['fields'][ $name ] ) ) {
		return '' === $name ? istota_connector_fields_error( new Istota_Fields_Error( 'validation_error', 'A path is a non-empty string such as blocks/0/items/3/label.', array( 'op' => 'ops[0]' ) ) ) : istota_connector_not_in_rest( $name );
	}
	$field = $target['fields'][ $name ];

	// 2. The value now, and the token the edit was made against.
	$value = istota_connector_normalized( $target['storage'], $field );
	if ( is_wp_error( $value ) ) {
		return $value;
	}
	$token = istota_connector_token_of( $value );
	if ( is_wp_error( $token ) ) {
		return $token;
	}
	if ( ! hash_equals( $token, (string) $input['token'] ) ) {
		return new WP_Error(
			'istota_stale_value',
			sprintf( '%s has changed since it was read. Read it again with fields-get and redo the edit against what it holds now.', $name ),
			array(
				'status' => 409,
				'params' => array( 'token' => $token ),
			)
		);
	}

	// What the editor would filter for this user, filtered here too.
	if ( ! acf_allow_unfiltered_html() ) {
		foreach ( $ops as $i => $op ) {
			if ( is_array( $op ) && array_key_exists( 'value', $op ) ) {
				$ops[ $i ]['value'] = istota_connector_kses_strings( $op['value'] );
			}
		}
	}

	// 3 to 5. Apply to a copy, validate what was written, check row counts.
	try {
		$result = istota_fields_apply( $field, $value, $ops );
	} catch ( Istota_Fields_Error $e ) {
		return istota_connector_fields_error( $e );
	}
	$errors = $result['errors'];
	$shape  = array();
	foreach ( $errors as $error ) {
		if ( null !== $error['op'] ) {
			$shape[ $error['op'] ] = true;
		}
	}
	$errors = array_merge( $errors, istota_connector_check_written( $result['written'], $shape ) );
	if ( $errors ) {
		$params = array();
		foreach ( $errors as $error ) {
			$at            = null === $error['op'] ? $error['path'] : $error['op'] . ' ' . $error['path'];
			$params[ $at ] = isset( $params[ $at ] ) ? $params[ $at ] . ' ' . $error['message'] : $error['message'];
		}
		return new WP_Error(
			'rest_invalid_param',
			'The edit was not applied: ' . implode( '; ', array_keys( $params ) ),
			array(
				'status' => 400,
				'params' => $params,
			)
		);
	}

	// 6 to 8. One write, the hooks, and the field read back.
	try {
		$stored = istota_fields_denormalize( $field, $result['value'] );
	} catch ( Istota_Fields_Error $e ) {
		return istota_connector_fields_error( $e );
	}
	update_field( $field['key'], $stored, $target['storage'] );
	istota_connector_after_write( $target );

	$after = istota_connector_normalized( $target['storage'], $field );
	if ( is_wp_error( $after ) ) {
		return $after;
	}
	$new_token = istota_connector_token_of( $after );
	if ( is_wp_error( $new_token ) ) {
		return $new_token;
	}
	$written_paths = array();
	foreach ( $result['written'] as $entry ) {
		$written_paths[ $entry['op'] ] = $entry['path'];
	}
	$changed  = array();
	$previous = array();
	foreach ( $ops as $i => $op ) {
		$path = isset( $written_paths[ $i ] ) ? $written_paths[ $i ] : $op['path'];
		$now  = null;
		if ( 'remove' !== $op['op'] ) {
			$node = istota_fields_resolve( $field, $after, istota_fields_parse_path( $path ) );
			$now  = $node['found'] ? $node['value'] : null;
		}
		$changed[]  = array(
			'op'    => $op['op'],
			'path'  => $path,
			'value' => $now,
		);
		$previous[] = $result['previous'][ $i ];
	}
	$out                     = istota_connector_fields_context( $target );
	$out['token']            = $new_token;
	$out['previous_token']   = $token;
	$out['changed']          = $changed;
	$out['previous']         = $previous;
	$out['missing_required'] = $result['missing_required'];
	return $out;
}

function istota_connector_fields_target_schema() {
	return array(
		'post_id' => array(
			'type'        => 'integer',
			'minimum'     => 1,
			'description' => 'The post, of any type. Give this or page.',
		),
		'page'    => istota_connector_page_schema(),
	);
}

/**
 * Any JSON value: what an op writes is checked against the field, not here.
 * The order is load-bearing. The run route sanitizes input to the first type
 * a value passes, so string first keeps "1" and "42" strings rather than a
 * boolean and an integer, and integer before boolean keeps 1 an integer.
 */
function istota_connector_any_schema() {
	return array( 'type' => array( 'string', 'integer', 'number', 'boolean', 'null', 'array', 'object' ) );
}

function istota_connector_fields_output_schema() {
	return array(
		'type'       => 'object',
		'properties' => array(
			'post_id'      => array( 'type' => 'string' ),
			'post_status'  => array( 'type' => array( 'string', 'null' ) ),
			'post_type'    => array( 'type' => array( 'string', 'null' ) ),
			'modified_gmt' => array( 'type' => array( 'string', 'null' ) ),
			'locked_by'    => array( 'type' => array( 'string', 'null' ) ),
			'token'        => array( 'type' => 'string' ),
		),
	);
}

function istota_connector_page_schema() {
	return array(
		'type'        => 'string',
		'description' => 'The options page slug, such as acf-options.',
		'pattern'     => '^[A-Za-z0-9_-]{1,64}$',
	);
}

function istota_connector_page_output_schema() {
	return array(
		'type'       => 'object',
		'properties' => array(
			'page'    => array( 'type' => 'string' ),
			'post_id' => array( 'type' => 'string' ),
			'fields'  => array(
				'type'                 => 'object',
				'additionalProperties' => true,
			),
		),
	);
}

function istota_connector_register_abilities() {
	wp_register_ability(
		'istota/options-get',
		array(
			'label'               => 'Read an ACF options page',
			'description'         => 'The values of an ACF options page\'s fields, from field groups shown in REST.',
			'category'            => 'istota',
			'input_schema'        => array(
				'type'                 => 'object',
				'properties'           => array( 'page' => istota_connector_page_schema() ),
				'required'             => array( 'page' ),
				'additionalProperties' => false,
			),
			'output_schema'       => istota_connector_page_output_schema(),
			'execute_callback'    => 'istota_connector_options_get',
			'permission_callback' => 'istota_connector_can_edit_options',
			'meta'                => array(
				'show_in_rest' => true,
				'annotations'  => array(
					'readonly'    => true,
					'destructive' => false,
					'idempotent'  => true,
				),
			),
		)
	);

	wp_register_ability(
		'istota/options-update',
		array(
			'label'               => 'Write ACF options page fields',
			'description'         => 'Replace the values of the named fields of an ACF options page, from field groups shown in REST. Other fields are left alone.',
			'category'            => 'istota',
			'input_schema'        => array(
				'type'                 => 'object',
				'properties'           => array(
					'page'   => istota_connector_page_schema(),
					'fields' => array(
						'type'                 => 'object',
						'minProperties'        => 1,
						'additionalProperties' => true,
					),
				),
				'required'             => array( 'page', 'fields' ),
				'additionalProperties' => false,
			),
			'output_schema'       => istota_connector_page_output_schema(),
			'execute_callback'    => 'istota_connector_options_update',
			'permission_callback' => 'istota_connector_can_edit_options',
			'meta'                => array(
				'show_in_rest' => true,
				'annotations'  => array(
					'readonly'    => false,
					'destructive' => false,
					'idempotent'  => true,
				),
			),
		)
	);

	wp_register_ability(
		'istota/network-sites',
		array(
			'label'               => 'List the network\'s sites',
			'description'         => 'The sites of this multisite network: id, domain, path, name, and whether each is public, archived or deleted.',
			'category'            => 'istota',
			'input_schema'        => array(
				'type'                 => 'object',
				'properties'           => array(
					'number' => array(
						'type'    => 'integer',
						'minimum' => 1,
						'maximum' => ISTOTA_CONNECTOR_MAX_SITES,
					),
				),
				'additionalProperties' => false,
				'default'              => array(),
			),
			'output_schema'       => array(
				'type'       => 'object',
				'properties' => array(
					'total' => array( 'type' => 'integer' ),
					'sites' => array(
						'type'  => 'array',
						'items' => array(
							'type'       => 'object',
							'properties' => array(
								'id'       => array( 'type' => 'integer' ),
								'domain'   => array( 'type' => 'string' ),
								'path'     => array( 'type' => 'string' ),
								'name'     => array( 'type' => 'string' ),
								'public'   => array( 'type' => 'boolean' ),
								'archived' => array( 'type' => 'boolean' ),
								'deleted'  => array( 'type' => 'boolean' ),
							),
						),
					),
				),
			),
			'execute_callback'    => 'istota_connector_network_sites',
			'permission_callback' => 'istota_connector_can_list_sites',
			'meta'                => array(
				'show_in_rest' => true,
				'annotations'  => array(
					'readonly'    => true,
					'destructive' => false,
					'idempotent'  => true,
				),
			),
		)
	);

	wp_register_ability(
		'istota/fields-get',
		array(
			'label'               => 'Read ACF fields for editing',
			'description'         => 'The ACF fields of a post or options page, from field groups shown in REST, each with a token. With a path, the value and definition of the node there, in the form istota/fields-edit accepts.',
			'category'            => 'istota',
			'input_schema'        => array(
				'type'                 => 'object',
				'properties'           => array_merge(
					istota_connector_fields_target_schema(),
					array(
						'path' => array(
							'type'        => 'string',
							'description' => 'A path inside one top-level field, such as blocks/0/items/3/label.',
							'minLength'   => 1,
							'maxLength'   => 1024,
						),
					)
				),
				'additionalProperties' => false,
			),
			'output_schema'       => istota_connector_fields_output_schema(),
			'execute_callback'    => 'istota_connector_fields_get',
			'permission_callback' => 'istota_connector_can_edit_fields',
			'meta'                => array(
				'show_in_rest' => true,
				'annotations'  => array(
					'readonly'    => true,
					'destructive' => false,
					'idempotent'  => true,
				),
			),
		)
	);

	wp_register_ability(
		'istota/fields-edit',
		array(
			'label'               => 'Edit inside an ACF field',
			'description'         => 'Apply set, insert, remove and move operations, in order, to one top-level ACF field of a post or options page. Refused unless the token matches the field as it is stored now. Only the values the operations write are validated; nothing they do not name changes.',
			'category'            => 'istota',
			'input_schema'        => array(
				'type'                 => 'object',
				'properties'           => array_merge(
					istota_connector_fields_target_schema(),
					array(
						'token' => array(
							'type'        => 'string',
							'description' => 'The token istota/fields-get returned for the field.',
							'minLength'   => 1,
							'maxLength'   => 128,
						),
						'ops'   => array(
							'type'     => 'array',
							'minItems' => 1,
							'maxItems' => ISTOTA_FIELDS_MAX_OPS,
							'items'    => array(
								'type'                 => 'object',
								'properties'           => array(
									'op'    => array(
										'type' => 'string',
										'enum' => array( 'set', 'insert', 'remove', 'move' ),
									),
									'path'  => array(
										'type'      => 'string',
										'minLength' => 1,
										'maxLength' => 1024,
									),
									'from'  => array(
										'type'      => 'string',
										'minLength' => 1,
										'maxLength' => 1024,
									),
									'value' => istota_connector_any_schema(),
								),
								'required'             => array( 'op', 'path' ),
								'additionalProperties' => false,
							),
						),
					)
				),
				'required'             => array( 'token', 'ops' ),
				'additionalProperties' => false,
			),
			'output_schema'       => istota_connector_fields_output_schema(),
			'execute_callback'    => 'istota_connector_fields_edit',
			'permission_callback' => 'istota_connector_can_edit_fields',
			'meta'                => array(
				'show_in_rest' => true,
				'annotations'  => array(
					'readonly'    => false,
					'destructive' => false,
					'idempotent'  => false,
				),
			),
		)
	);
}
