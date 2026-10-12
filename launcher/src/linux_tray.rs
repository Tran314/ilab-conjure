//! Preflight the same AppIndicator loader used by tray-icon/libappindicator-sys.
//! Their lazy loader panics on a missing library or symbol, rather than returning
//! the tray builder's Result. Convert those failures before touching that loader.
use anyhow::{anyhow, bail, Result};
use std::{
    ffi::{CStr, CString},
    ptr::NonNull,
};

pub struct AppIndicatorLibrary(NonNull<libc::c_void>);
impl Drop for AppIndicatorLibrary {
    fn drop(&mut self) {
        unsafe {
            libc::dlclose(self.0.as_ptr());
        }
    }
}

pub fn preflight_appindicator() -> Result<AppIndicatorLibrary> {
    load_candidates(&["libayatana-appindicator3.so.1", "libappindicator3.so.1"])
}

fn load_candidates(names: &[&str]) -> Result<AppIndicatorLibrary> {
    let mut errors = Vec::new();
    for name in names {
        let c_name = CString::new(*name)?;
        let handle = unsafe { libc::dlopen(c_name.as_ptr(), libc::RTLD_NOW | libc::RTLD_LOCAL) };
        let Some(handle) = NonNull::new(handle) else {
            let detail = unsafe { libc::dlerror() };
            let detail = if detail.is_null() {
                "not found".into()
            } else {
                unsafe { CStr::from_ptr(detail) }
                    .to_string_lossy()
                    .into_owned()
            };
            errors.push(format!("{name}: {detail}"));
            continue;
        };
        let library = AppIndicatorLibrary(handle);
        for symbol in [
            "app_indicator_new",
            "app_indicator_set_status",
            "app_indicator_set_menu",
            "app_indicator_set_label",
            "app_indicator_set_icon_theme_path",
            "app_indicator_set_icon_full",
        ] {
            let c_symbol = CString::new(symbol)?;
            if unsafe { libc::dlsym(handle.as_ptr(), c_symbol.as_ptr()) }.is_null() {
                // Do not fall through to another library: libappindicator-sys would
                // still select this first loadable, incompatible library and panic.
                bail!("Incompatible AppIndicator library {name}: missing {symbol}");
            }
        }
        return Ok(library);
    }
    Err(anyhow!(
        "AppIndicator library unavailable; install libayatana-appindicator3-1. {}",
        errors.join("; ")
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn missing_indicator_library_is_recoverable_error() {
        assert!(load_candidates(&["/nonexistent/ilab-test-appindicator.so"]).is_err());
    }
    #[test]
    fn wrong_library_is_rejected_before_lazy_loader_panics() {
        let error = load_candidates(&["libc.so.6"])
            .err()
            .expect("missing AppIndicator symbols");
        assert!(error.to_string().contains("Incompatible AppIndicator"));
    }
}
