//! Launch at login (`SMAppService.mainApp`), and whether this launch was one.
//!
//! Launched at login, the app starts the VM and opens no window. macOS marks
//! such a launch on the "open application" Apple event (`keyAEPropData` =
//! `keyAELaunchedAsLogInItem`), which is still the current event while the
//! app finishes launching, where Tauri's `setup` runs.

use objc2::msg_send;
use objc2::rc::Retained;
use objc2_foundation::{NSAppleEventDescriptor, NSAppleEventManager};
use objc2_service_management::{SMAppService, SMAppServiceStatus};

/// `keyAEPropData`, `'prdt'`.
const KEY_AE_PROP_DATA: u32 = u32::from_be_bytes(*b"prdt");
/// `keyAELaunchedAsLogInItem`, `'lgit'`.
const KEY_AE_LAUNCHED_AS_LOGIN_ITEM: u32 = u32::from_be_bytes(*b"lgit");

pub fn launched_at_login() -> bool {
    let manager = NSAppleEventManager::sharedAppleEventManager();
    let Some(event) = manager.currentAppleEvent() else {
        return false;
    };
    // The bindings gate this method on CoreServices' AEKeyword type, a u32.
    // SAFETY: `paramDescriptorForKeyword:` takes a FourCharCode and returns
    // a descriptor or nil.
    let param: Option<Retained<NSAppleEventDescriptor>> =
        unsafe { msg_send![&*event, paramDescriptorForKeyword: KEY_AE_PROP_DATA] };
    let Some(param) = param else {
        return false;
    };
    param.enumCodeValue() == KEY_AE_LAUNCHED_AS_LOGIN_ITEM
}

pub fn launch_at_login_enabled() -> bool {
    // SAFETY: plain ServiceManagement calls with no arguments.
    unsafe { SMAppService::mainAppService().status() == SMAppServiceStatus::Enabled }
}

/// Turns launch at login on or off. macOS may ask the user to approve the
/// login item in System Settings; that is reported as an error to show.
pub fn set_launch_at_login(on: bool) -> Result<(), String> {
    // SAFETY: as above; the error is an NSError the bindings own.
    unsafe {
        let service = SMAppService::mainAppService();
        let result = if on { service.registerAndReturnError() } else { service.unregisterAndReturnError() };
        result.map_err(|err| err.localizedDescription().to_string())?;
        if on && service.status() == SMAppServiceStatus::RequiresApproval {
            SMAppService::openSystemSettingsLoginItems();
            return Err("Allow Istota under Login Items in System Settings.".into());
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_four_character_codes_are_apples() {
        assert_eq!(KEY_AE_PROP_DATA, 0x7072_6474);
        assert_eq!(KEY_AE_LAUNCHED_AS_LOGIN_ITEM, 0x6c67_6974);
    }

    #[test]
    fn a_test_process_was_not_launched_at_login() {
        assert!(!launched_at_login());
    }
}
