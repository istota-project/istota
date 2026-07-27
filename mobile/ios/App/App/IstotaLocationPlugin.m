//
//  IstotaLocationPlugin.m
//  Istota
//

#import "IstotaLocationPlugin.h"

// `resolve:` / `reject:` are Swift extensions on CAPPluginCall, exposed to
// Objective-C through the generated -Swift.h. The typed JS accessors
// (getString:defaultValue: and friends) live in CAPBridgedJSTypes.h, which is
// deliberately excluded from the Swift module map and is the Objective-C entry
// point per its own header comment.
#import <Capacitor/Capacitor-Swift.h>
#import <Capacitor/CAPBridgedJSTypes.h>

@implementation IstotaLocationPlugin

- (void)load {
    NSLog(@"[IstotaLocation] plugin loaded (pure Objective-C)");
}

- (void)ping:(CAPPluginCall *)call {
    NSString *echo = [call getString:@"value" defaultValue:@""];

    [call resolve:@{
        @"pong": @YES,
        @"echo": echo ?: @"",
        @"language": @"objective-c",
        @"receivedAt": @([[NSDate date] timeIntervalSince1970]),
    }];
}

@end
