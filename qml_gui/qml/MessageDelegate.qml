import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0

Rectangle {
    id: delegateRoot
    
    // Bind width to ListView's width
    width: ListView.view ? ListView.view.width : parent ? parent.width : 0
    height: contentColumn.implicitHeight + 20
    
    // Read model properties
    property string role: model.role || ""
    property string content: model.content || ""
    property string htmlContent: model.htmlContent || ""
    property string toolName: model.toolName || ""
    property bool isFinal: model.isFinal || false
    property string reasoning: model.reasoning || ""
    property var toolCalls: model.toolCalls || []
    property string createdAt: model.createdAt || ""
    
    // Conditional styling based on role and toolName
    color: getBackgroundColor()
    border.color: getBorderColor()
    border.width: 2
    radius: 8
    
    // Helper functions for colors
    function getBackgroundColor() {
        if (role === "user") return "#FFF0F5"
        else if (role === "assistant") {
            if (toolName === "Final" || toolName === "FinalReport") return "#dbeafe"
            else if (toolName === "RequestUserInteraction") return "#e0f7fa"
            else return "#e6f3ff"
        }
        else if (role === "tool") return "#e6ffe6"
        else if (role === "system") return "#fff9e6"
        else return "#ffffff"
    }
    
    function getBorderColor() {
        if (role === "user") return "#FF69B4"
        else if (role === "assistant") {
            if (toolName === "Final" || toolName === "FinalReport") return "#1e3a8a"
            else if (toolName === "RequestUserInteraction") return "#00bcd4"
            else return "#99ccff"
        }
        else if (role === "tool") return "#00aa00"
        else if (role === "system") return "#ffcc00"
        else return "#cccccc"
    }
    
    Column {
        id: contentColumn
        anchors.fill: parent
        anchors.margins: 10
        spacing: 8
        
        // Header row: role and timestamp
        RowLayout {
            width: parent.width
            
            Text {
                text: "<b>" + (role || "unknown") + "</b>"
                font.pixelSize: 14
                color: "darkblue"
                font.bold: true
            }
            
            Text {
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignRight
                text: createdAt ? new Date(createdAt).toLocaleString() : ""
                font.pixelSize: 10
                color: "gray"
            }
        }
        
        // Tool name indicator for special messages
        Text {
            visible: toolName && toolName !== ""
            text: "Tool: <b>" + toolName + "</b>"
            font.pixelSize: 12
            color: "darkgreen"
            textFormat: Text.RichText
        }
        
        // Final Answer indicator
        Text {
            visible: isFinal
            text: "✓ Final Answer"
            font.pixelSize: 12
            color: "#1e3a8a"
            font.bold: true
        }
        
        // Main content - prefer HTML if available (markdown rendered)
        Text {
            width: parent.width
            text: htmlContent || content || ""
            textFormat: htmlContent ? Text.RichText : Text.PlainText
            wrapMode: Text.WordWrap
            font.pixelSize: 12
            onLinkActivated: Qt.openUrlExternally(link)
        }
        
        // Reasoning section (for assistant messages with reasoning)
        Rectangle {
            visible: reasoning && reasoning !== "" && role === "assistant"
            width: parent.width
            height: reasoningText.implicitHeight + 10
            color: "#f5f5f5"
            border.color: "#cccccc"
            border.width: 1
            radius: 4
            
            Text {
                id: reasoningText
                anchors.fill: parent
                anchors.margins: 5
                text: "<b>Reasoning:</b> " + reasoning
                textFormat: Text.RichText
                wrapMode: Text.WordWrap
                font.pixelSize: 11
                color: "#333333"
                font.italic: false
            }
        }
        
        // Tool calls section (for assistant messages with tool calls)
        Column {
            visible: toolCalls && toolCalls.length > 0 && role === "assistant"
            width: parent.width
            spacing: 5
            
            Text {
                text: "Tool Calls:"
                font.pixelSize: 11
                color: "#666666"
                font.bold: true
            }
            
            Repeater {
                model: toolCalls
                
                Rectangle {
                    width: parent.width
                    height: toolCallText.implicitHeight + 10
                    color: "#f0f8ff"
                    border.color: "#99ccff"
                    border.width: 1
                    radius: 4
                    
                    Text {
                        id: toolCallText
                        anchors.fill: parent
                        anchors.margins: 5
                        text: {
                            var toolCall = modelData
                            var name = toolCall.name || "unknown"
                            var args = toolCall.arguments || {}
                            return "<b>" + name + "</b>: " + JSON.stringify(args, null, 2)
                        }
                        textFormat: Text.RichText
                        wrapMode: Text.WordWrap
                        font.pixelSize: 10
                        color: "#333333"
                        font.family: "Monospace"
                    }
                }
            }
        }
    }
}