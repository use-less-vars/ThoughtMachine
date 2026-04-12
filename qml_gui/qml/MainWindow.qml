import QtQuick 6.0
import QtQuick.Window 2.15
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0

Window {
    width: 800
    height: 600
    title: "ThoughtMachine QML"
    visible: true
    
    Rectangle {
        anchors.fill: parent
        
        ColumnLayout {
            anchors.fill: parent
            spacing: 5
            
            RowLayout {
                Layout.fillWidth: true
                spacing: 5
                
                Text {
                    Layout.alignment: Qt.AlignHCenter
                    Layout.fillWidth: true
                    text: "Conversation History"
                    font.bold: true
                    font.pixelSize: 20
                    padding: 10
                }
                
                Button {
                    text: "Settings"
                    onClicked: {
                        configDialog.open()
                    }
                }
            }
            
            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                border.color: "gray"
                border.width: 1
                
                ScrollView {
                    anchors.fill: parent
                    anchors.margins: 5
                    
                    ListView {
                        id: conversationView
                        model: conversationModel
                        spacing: 10
                        delegate: MessageDelegate {}
                        onCountChanged: {
                            // Auto-scroll to bottom when new messages added
                            positionViewAtEnd()
                        }
                    }
                }
            }
            
            // Status bar
            Rectangle {
                Layout.fillWidth: true
                height: 30
                color: "lightgray"
                
                Text {
                    anchors.fill: parent
                    anchors.leftMargin: 10
                    verticalAlignment: Text.AlignVCenter
                    text: "Status: Ready"
                }
            }
            
            // Input area
            Rectangle {
                Layout.fillWidth: true
                height: 60
                color: "#f0f0f0"
                border.color: "gray"
                border.width: 1
                
                RowLayout {
                    anchors.fill: parent
                    anchors.margins: 5
                    spacing: 5
                    
                    TextField {
                        id: messageInput
                        Layout.fillWidth: true
                        placeholderText: "Type your message here..."
                        Keys.onReturnPressed: {
                            if (event.modifiers & Qt.ControlModifier) {
                                // Ctrl+Enter for new line
                                insert(cursorPosition, "\n")
                            } else {
                                sendButton.clicked()
                                event.accepted = true
                            }
                        }
                        Keys.onEnterPressed: {
                            sendButton.clicked()
                            event.accepted = true
                        }
                    }
                    
                    Button {
                        id: sendButton
                        text: "Send"
                        enabled: messageInput.text.trim().length > 0
                        onClicked: {
                            var text = messageInput.text.trim()
                            if (text.length > 0) {
                                presenter.on_user_input(text)
                                messageInput.clear()
                            }
                        }
                    }
                }
            }
        }
    }

    ConfigDialog {
        id: configDialog
    }

    // Component replaced by MessageDelegate.qml
}