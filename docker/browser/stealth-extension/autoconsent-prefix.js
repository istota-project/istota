(function() {
    // Message handler that autoconsent calls to communicate
    window.autoconsentSendMessage = function(msg) {
        var type = msg.type;
        if (type === 'init') {
            // Respond with config to start detection
            setTimeout(function() {
                if (window.autoconsentReceiveMessage) {
                    window.autoconsentReceiveMessage({
                        type: 'initResp',
                        config: {
                            enabled: true,
                            autoAction: 'optOut',
                            disabledCmps: [],
                            enablePrehide: true,
                            enableCosmeticRules: true,
                            detectRetries: 20,
                            isMainWorld: true,
                            enableFilterlist: false
                        }
                    });
                }
            }, 0);
        } else if (type === 'eval') {
            // Evaluate JS and respond
            var id = msg.id;
            var result = false;
            try { result = !!eval(msg.code); } catch(e) {}
            setTimeout(function() {
                if (window.autoconsentReceiveMessage) {
                    window.autoconsentReceiveMessage({
                        type: 'evalResp',
                        id: id,
                        result: result
                    });
                }
            }, 0);
        }
        // Other message types (cmpDetected, optOutResult, etc.) are just logged
    };

    // Inject the autoconsent bundle
