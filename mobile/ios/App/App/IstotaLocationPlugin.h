//
//  IstotaLocationPlugin.h
//  Istota
//
//  Stage 0 feasibility spike: the smallest pure-Objective-C CAPPlugin that
//  proves the shape the Overland port depends on. Stage 2 fills this class in
//  with the ported GLManager; the registration wiring stays as-is.
//

#import <Foundation/Foundation.h>
#import <Capacitor/Capacitor.h>

NS_ASSUME_NONNULL_BEGIN

@interface IstotaLocationPlugin : CAPPlugin

/// Round-trips a value through the native layer so the JS side can prove the
/// bridge reached Objective-C, not just that the plugin object exists.
- (void)ping:(CAPPluginCall *)call;

@end

NS_ASSUME_NONNULL_END
