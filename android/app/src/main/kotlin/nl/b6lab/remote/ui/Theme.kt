package nl.b6lab.remote.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import nl.b6lab.remote.R

/** The stage look, values from the generated B6Tokens: gunmetal and panel greys, silkscreen text, goldenrod accent, a red LED. */
object B6Colors {
    val Gunmetal = B6Tokens.Colors.Canvas
    val Panel = B6Tokens.Colors.Panel
    val Silkscreen = B6Tokens.Colors.Text
    val SilkscreenDim = B6Tokens.Colors.TextMuted
    val Amber = B6Tokens.Colors.Accent
    val LedRed = B6Tokens.Colors.LedOn
    val LedOff = B6Tokens.Colors.LedOff
}

/**
 * Barlow Condensed for instrument text (Regular, Medium, SemiBold) and Barlow (normal width) for body text.
 * Both SIL OFL 1.1, bundled offline (see res/raw/barlow_condensed_ofl.txt and the Barlow licence text).
 */
object B6Fonts {
    val Regular = FontFamily(Font(R.font.barlow_condensed_regular, FontWeight.Normal))
    val Medium = FontFamily(Font(R.font.barlow_condensed_medium, FontWeight.Medium))
    val SemiBold = FontFamily(Font(R.font.barlow_condensed_semibold, FontWeight.SemiBold))
    val Body = FontFamily(
        Font(R.font.barlow_regular, FontWeight.Normal),
        Font(R.font.barlow_medium, FontWeight.Medium),
    )
}

/** Text styles of the app; sizes are set where they are used. */
object B6Text {
    val Label = TextStyle(fontFamily = B6Fonts.Regular, fontWeight = FontWeight.Normal, color = B6Colors.Silkscreen)
    val Button = TextStyle(fontFamily = B6Fonts.Medium, fontWeight = FontWeight.Medium, color = B6Colors.Silkscreen)
    val Title = TextStyle(fontFamily = B6Fonts.SemiBold, fontWeight = FontWeight.SemiBold, color = B6Colors.Silkscreen)
    val Value = Button.copy(fontFeatureSettings = "tnum")
}

@Composable
fun B6Theme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = darkColorScheme(
            background = B6Colors.Gunmetal,
            surface = B6Colors.Panel,
            primary = B6Colors.Amber,
            onPrimary = B6Colors.Gunmetal,
            onBackground = B6Colors.Silkscreen,
            onSurface = B6Colors.Silkscreen,
            surfaceContainerLow = B6Colors.Panel,
            surfaceContainerHigh = B6Colors.Panel,
        ),
        typography = Typography().let { t ->
            t.copy(
                bodyLarge = t.bodyLarge.copy(fontFamily = B6Fonts.Body),
                bodyMedium = t.bodyMedium.copy(fontFamily = B6Fonts.Body),
                labelLarge = t.labelLarge.copy(fontFamily = B6Fonts.Medium),
            )
        },
        content = content,
    )
}
