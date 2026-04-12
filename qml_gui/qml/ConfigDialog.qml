import QtQuick 6.0
import QtQuick.Window 2.15
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0

Dialog {
    id: configDialog
    title: "Configuration"
    modal: true
    standardButtons: Dialog.Ok | Dialog.Cancel
    anchors.centerIn: parent
    width: 500
    height: 400

    property var currentConfig: ({})

    onAccepted: {
        // Save configuration
        var config = {
            "api_key": apiKeyField.text,
            "model": modelField.text,
            "base_url": baseUrlField.text,
            "provider_type": providerTypeField.currentText
        }
        
        // Update configuration via presenter
        presenter.update_config_from_gui(config)
        
        // Save to user config file
        if (typeof presenter.save_user_config === "function") {
            // Save the full configuration (presenter.config includes merged values)
            presenter.save_user_config(presenter.config)
        }
    }

    onRejected: {
        // Discard changes
        loadConfig()
    }

    function loadConfig() {
        // Load current configuration from presenter
        if (typeof presenter.config !== "undefined") {
            currentConfig = presenter.config
            apiKeyField.text = currentConfig.api_key || ""
            modelField.text = currentConfig.model || ""
            baseUrlField.text = currentConfig.base_url || ""
            
            // Set provider type
            var providerIndex = providerTypeModel.indexOf(currentConfig.provider_type || "")
            if (providerIndex >= 0) {
                providerTypeField.currentIndex = providerIndex
            }
        }
    }

    Component.onCompleted: {
        loadConfig()
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 10

        GroupBox {
            title: "API Configuration"
            Layout.fillWidth: true

            GridLayout {
                columns: 2
                anchors.fill: parent
                rowSpacing: 5
                columnSpacing: 10

                Label { text: "API Key:" }
                TextField {
                    id: apiKeyField
                    Layout.fillWidth: true
                    echoMode: TextInput.Password
                    placeholderText: "Enter your API key"
                }

                Label { text: "Model:" }
                TextField {
                    id: modelField
                    Layout.fillWidth: true
                    placeholderText: "e.g., gpt-4-turbo, deepseek-chat"
                }

                Label { text: "Base URL:" }
                TextField {
                    id: baseUrlField
                    Layout.fillWidth: true
                    placeholderText: "e.g., https://api.openai.com/v1"
                }

                Label { text: "Provider Type:" }
                ComboBox {
                    id: providerTypeField
                    Layout.fillWidth: true
                    model: ListModel {
                        id: providerTypeModel
                        ListElement { text: "openai" }
                        ListElement { text: "openai_compatible" }
                        ListElement { text: "anthropic" }
                    }
                }
            }
        }

        GroupBox {
            title: "Advanced Settings"
            Layout.fillWidth: true
            visible: false  // Hide for now, can be expanded later

            GridLayout {
                columns: 2
                anchors.fill: parent
                rowSpacing: 5
                columnSpacing: 10

                Label { text: "Temperature:" }
                Slider {
                    id: temperatureSlider
                    from: 0.0
                    to: 2.0
                    value: 0.7
                    stepSize: 0.1
                }

                Label { text: "Max Tokens:" }
                TextField {
                    id: maxTokensField
                    validator: IntValidator { bottom: 1; top: 100000 }
                    text: "2000"
                }
            }
        }

        Item {
            Layout.fillHeight: true
        }
    }
}