#include "cp_adc_level.h"

bool cp_adc_level_high(int adc_raw, bool previous_level, int low_thresh, int high_thresh) {
  if (adc_raw >= high_thresh) {
    return true;
  }
  if (adc_raw <= low_thresh) {
    return false;
  }
  return previous_level;
}
