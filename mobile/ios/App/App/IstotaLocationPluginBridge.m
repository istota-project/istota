//
//  IstotaLocationPluginBridge.m
//  Istota
//
//  Capacitor's CAP_PLUGIN macro expands to `@interface <name> : NSObject`, which
//  would collide with the real `@interface IstotaLocationPlugin : CAPPlugin`.
//  The macro therefore has to live in its own translation unit that never
//  imports IstotaLocationPlugin.h — the same split Capacitor uses for its Swift
//  plugins, where the .m sees only the macro and the class is defined elsewhere.
//  Objective-C categories are attached by name at runtime, so the two halves
//  meet at load time rather than at compile time.
//

#import <Foundation/Foundation.h>
#import <Capacitor/Capacitor.h>

CAP_PLUGIN(IstotaLocationPlugin, "IstotaLocation",
           CAP_PLUGIN_METHOD(ping, CAPPluginReturnPromise);
)
